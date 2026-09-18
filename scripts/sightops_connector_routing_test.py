"""Testes do alocador de tabelas de roteamento por conector (ops/connector_routing).

Padrao local: autoexecutavel, `def main()` com asserts, `sys.exit(1)` em falha.
Nao toca em rede -- so a logica pura de alocacao.
"""
import ipaddress
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops.connector_routing import allocator as A
from ops.connector_routing import state as S
from ops.connector_routing import system_ops as O
from ops.connector_routing import provisioner as P


def test_determinismo_e_idempotencia():
    state = {}
    a1 = A.allocate("conectorA", state)
    a2 = A.allocate("conectorA", state)
    assert a1 == a2, "mesma alocacao pro mesmo conector"
    assert len(state["connectors"]) == 1, "nao duplica conector no estado"
    # derive puro bate com o alocado
    assert A.derive(a1["index"], "conectorA") == a1


def test_sem_colisao_entre_conectores():
    state = {}
    allocs = [A.allocate(f"c{i:03d}", state) for i in range(50)]
    for campo in ("index", "table_id", "ifname", "listen_port", "server_ip", "fwmark"):
        valores = [a[campo] for a in allocs]
        assert len(set(valores)) == len(valores), f"colisao em {campo}"
    # tabelas longe das reservadas do kernel (0,253,254,255)
    assert all(a["table_id"] >= A.TABLE_BASE for a in allocs)
    assert all(a["table_id"] not in (0, 253, 254, 255) for a in allocs)


def test_transito_31_correto():
    state = {}
    a1 = A.allocate("x1", state)
    a2 = A.allocate("x2", state)
    # servidor e peer sao vizinhos dentro do mesmo /31
    n1 = ipaddress.ip_network(a1["transfer_cidr"], strict=True)
    assert n1.prefixlen == 31
    assert ipaddress.ip_address(a1["server_ip"]) in n1
    assert ipaddress.ip_address(a1["peer_ip"]) in n1
    assert int(ipaddress.ip_address(a1["peer_ip"])) - int(ipaddress.ip_address(a1["server_ip"])) == 1
    # /31 de conectores diferentes nao se sobrepoem
    n2 = ipaddress.ip_network(a2["transfer_cidr"], strict=True)
    assert not n1.overlaps(n2), "transito de conectores diferentes nao pode sobrepor"
    # dentro do bloco reservado
    bloco = ipaddress.ip_network(A.TRANSFER_BLOCK)
    assert n1.subnet_of(bloco) and n2.subnet_of(bloco)


def test_persistencia_preserva_index():
    # simula reload: um conector ja existente mantem o index; um novo pega o proximo livre
    state = {"connectors": {"antigo": {"index": 7}}}
    a_antigo = A.allocate("antigo", state)
    assert a_antigo["index"] == 7, "index persistido tem que ser respeitado"
    a_novo = A.allocate("novo", state)
    assert a_novo["index"] != 7 and a_novo["index"] >= 1
    # nao reaproveita index de quem ja tem
    assert a_novo["index"] not in (7,)


def test_ifname_dentro_do_limite_linux():
    state = {}
    a = A.allocate("qualquer", state)
    assert len(a["ifname"]) <= 15, "nome de interface Linux tem limite de 15 chars"
    assert a["ifname"].startswith(A.IFNAME_PREFIX)


def test_plan_para_varios():
    state = {}
    plan = A.plan_for_connectors(["cB", "cA", "cA"], state)
    assert set(plan.keys()) == {"cA", "cB"}
    # index estavel e sem colisao
    assert plan["cA"]["index"] != plan["cB"]["index"]


# ---- estado (JSON) ----

def test_state_roundtrip_e_default():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "state.json")
        assert S.load_state(path) == {"connectors": {}}, "arquivo inexistente -> vazio"
        st = {"connectors": {}}
        A.allocate("c1", st)
        A.allocate("c2", st)
        S.save_state(st, path)
        again = S.load_state(path)
        assert again == st, "round-trip preserva o estado"
        # reload + alocar de novo mantem o index (persistencia real)
        a1 = A.allocate("c1", again)
        assert a1["index"] == st["connectors"]["c1"]["index"]


# ---- camada de execucao (build/run) ----

def _alloc(cid="siteX"):
    return A.allocate(cid, {})


def test_build_steps_conteudo_correto():
    a = A.derive(3, "site3")
    steps = O.build_provision_steps(a, "PRIVKEY", "PEERPUB", ["192.168.10.0/24", "10.0.0.0/24"])
    flat = [" ".join(s["argv"]) for s in steps]
    joined = "\n".join(flat)
    assert f"ip link add dev {a['ifname']} type wireguard" in joined
    assert f"listen-port {a['listen_port']}" in joined
    assert "peer PEERPUB allowed-ips 192.168.10.0/24,10.0.0.0/24" in joined
    # rotas nas DUAS lans, na tabela do conector
    assert f"ip route replace 192.168.10.0/24 dev {a['ifname']} table {a['table_id']}" in joined
    assert f"ip route replace 10.0.0.0/24 dev {a['ifname']} table {a['table_id']}" in joined
    # regra por IP de origem, na tabela do conector, com pref fixa
    pref = str(O.RULE_PREF_BASE + a["index"])
    assert f"ip rule add from {a['server_ip']} table {a['table_id']} pref {pref}" in joined
    # del da pref antes do add -> idempotente
    assert f"ip rule del pref {pref}" in joined


def test_chave_privada_nunca_no_argv():
    a = _alloc()
    steps = O.build_provision_steps(a, "SUPERSECRETKEY", "PEERPUB", ["192.168.1.0/24"])
    for s in steps:
        assert "SUPERSECRETKEY" not in " ".join(s["argv"]), "chave nao pode aparecer no argv (ps/log)"
    # a chave existe, mas so no stdin do passo de private-key
    key_steps = [s for s in steps if s.get("stdin") == "SUPERSECRETKEY"]
    assert len(key_steps) == 1 and "/dev/stdin" in key_steps[0]["argv"]
    # redact nao vaza stdin
    assert all("SUPERSECRETKEY" not in " ".join(a2) for a2 in O.redact_steps(steps))


def test_run_steps_fake_ok_e_falha():
    a = _alloc()
    steps = O.build_provision_steps(a, "K", "P", ["192.168.1.0/24"])
    chamadas = []

    def fake_ok(argv, stdin):
        chamadas.append((argv, stdin))
        return (0, "")

    log = O.run_steps(steps, runner=fake_ok)
    assert len(chamadas) == len(steps), "todos os passos rodaram"
    assert all(e["rc"] == 0 for e in log)
    assert all("K" not in " ".join(e["argv"]) for e in log), "log nao vaza a chave"

    # passo ok_if_exists que falha NAO aborta; passo normal que falha aborta
    def fake_fail_link(argv, stdin):
        if argv[:3] == ["ip", "link"] and "add" in argv:
            return (2, "RTNETLINK answers: File exists")  # ok_if_exists -> tolerado
        return (0, "")

    O.run_steps(steps, runner=fake_fail_link)  # nao levanta

    def fake_fail_route(argv, stdin):
        if argv[:2] == ["ip", "route"]:
            return (2, "erro grave")  # passo normal -> tem que abortar
        return (0, "")

    raised = False
    try:
        O.run_steps(steps, runner=fake_fail_route)
    except RuntimeError:
        raised = True
    assert raised, "falha em passo obrigatorio tem que abortar"


def test_teardown_desfaz_na_ordem():
    a = _alloc()
    steps = O.build_teardown_steps(a)
    flat = [" ".join(s["argv"]) for s in steps]
    assert flat[0].startswith("ip rule del pref")
    assert any(f"ip route flush table {a['table_id']}" in f for f in flat)
    assert flat[-1] == f"ip link del dev {a['ifname']}"


# ---- provisionador (reconcile) ----

def _full_current_for(alloc, cidrs):
    """Retrato de servidor onde ESTE conector ja esta 100% provisionado."""
    pref = O.RULE_PREF_BASE + alloc["index"]
    return {
        "interfaces": {alloc["ifname"]},
        "rules": {pref: {"from": alloc["server_ip"], "table": str(alloc["table_id"])}},
        "routes": {str(alloc["table_id"]): {P._canon(c) for c in cidrs}},
        "peers": {alloc["ifname"]: {"allowed_ips": {P._canon(c) for c in cidrs}}},
    }


def test_diff_detecta_faltas_e_ok():
    a = A.derive(4, "c4")
    cidrs = ["192.168.10.0/24"]
    vazio = {"interfaces": set(), "rules": {}, "routes": {}, "peers": {}}
    faltas = P.diff_connector(a, cidrs, vazio)
    assert "interface" in faltas and "rule" in faltas and "route:192.168.10.0/24" in faltas
    # tudo presente -> nada faltando
    assert P.diff_connector(a, cidrs, _full_current_for(a, cidrs)) == []
    # regra apontando pra tabela errada conta como falta
    cur = _full_current_for(a, cidrs)
    cur["rules"][O.RULE_PREF_BASE + a["index"]]["table"] = "999"
    assert "rule" in P.diff_connector(a, cidrs, cur)


def test_read_current_parser():
    a = A.derive(2, "c2")
    ifname, table = a["ifname"], a["table_id"]
    saidas = {
        "wg show interfaces": (0, f"wg-sightops {ifname}\n"),
        "ip rule show": (0, f"0:\tfrom all lookup local\n{O.RULE_PREF_BASE + 2}:\tfrom {a['server_ip']} lookup {table}\n32766:\tfrom all lookup main\n"),
        f"ip route show table {table}": (0, f"192.168.10.0/24 dev {ifname} scope link\n"),
        f"wg show {ifname} allowed-ips": (0, f"SOMEPUBKEYbase64=\t192.168.10.0/24\n"),
    }

    def fake_read(argv):
        return saidas.get(" ".join(argv), (0, ""))

    cur = P.read_current(fake_read, [table])
    assert ifname in cur["interfaces"] and "wg-sightops" not in cur["interfaces"]
    assert cur["rules"][O.RULE_PREF_BASE + 2] == {"from": a["server_ip"], "table": str(table)}
    assert "192.168.10.0/24" in cur["routes"][str(table)]
    assert "192.168.10.0/24" in cur["peers"][ifname]["allowed_ips"]


def test_reconcile_dry_run_e_ok():
    state = {}
    conns = [
        {"connector_id": "mata-grande", "pubkey": "PUBA", "site_cidrs": ["192.168.10.0/24"]},
        {"connector_id": "porto-real", "pubkey": "PUBB", "site_cidrs": ["192.168.10.0/24"]},
    ]
    # servidor vazio -> os dois precisam aplicar
    empty_read = lambda argv: (0, "")
    res = P.reconcile(conns, state, private_key="SECRET", read_runner=empty_read, dry_run=True)
    assert all(r["action"] == "would-apply" for r in res)
    # mesmo IP, tabelas/interfaces diferentes -> isolados
    ifs = {r["ifname"] for r in res}
    tbs = {r["table"] for r in res}
    assert len(ifs) == 2 and len(tbs) == 2
    # dry-run nao vaza a chave
    for r in res:
        assert all("SECRET" not in " ".join(c) for c in r["cmds"])


def test_reconcile_pula_quem_ja_esta_ok():
    state = {}
    conn = {"connector_id": "mata-grande", "pubkey": "PUBA", "site_cidrs": ["192.168.10.0/24"]}
    a = A.allocate("mata-grande", state)
    full = _full_current_for(a, ["192.168.10.0/24"])

    def read_full(argv):
        cmd = " ".join(argv)
        if cmd == "wg show interfaces":
            return (0, " ".join(full["interfaces"]))
        if cmd == "ip rule show":
            pref = O.RULE_PREF_BASE + a["index"]
            return (0, f"{pref}:\tfrom {a['server_ip']} lookup {a['table_id']}\n")
        if cmd == f"ip route show table {a['table_id']}":
            return (0, "192.168.10.0/24 dev %s\n" % a["ifname"])
        if cmd == f"wg show {a['ifname']} allowed-ips":
            return (0, "PUB\t192.168.10.0/24\n")
        return (0, "")

    res = P.reconcile([conn], state, private_key="SECRET", read_runner=read_full, dry_run=True)
    assert res[0]["action"] == "ok", "conector ja provisionado nao deve reaplicar"


def test_connectors_from_store_rows():
    rows = [
        {"id": "c-ok", "name": "Mata Grande", "tunnel": {
            "enabled": True, "type": "wireguard", "client_public_key": "PUB=",
            "client_address": "10.9.0.5", "client_lans": ["192.168.10.0/24", "10.0.0.0/24"]}},
        {"id": "c-sem-tunel", "tunnel": {"enabled": False}},
        {"id": "c-sem-pub", "tunnel": {"enabled": True, "type": "wireguard", "client_lans": ["192.168.1.0/24"]}},
        "lixo-nao-dict",
    ]
    out = P.connectors_from_store_rows(rows)
    assert len(out) == 1, "so o conector valido entra"
    c = out[0]
    assert c["connector_id"] == "c-ok" and c["pubkey"] == "PUB="
    # client_address vira /32, lans mantem
    assert "10.9.0.5/32" in c["site_cidrs"]
    assert "192.168.10.0/24" in c["site_cidrs"] and "10.0.0.0/24" in c["site_cidrs"]


def test_reconcile_aplica_de_verdade_com_fake():
    state = {}
    conn = {"connector_id": "c1", "pubkey": "PUB", "site_cidrs": ["192.168.10.0/24"]}
    empty_read = lambda argv: (0, "")
    executados = []

    def fake_run(argv, stdin):
        executados.append((argv, stdin))
        return (0, "")

    res = P.reconcile([conn], state, private_key="SECRET", read_runner=empty_read,
                      run_runner=fake_run, dry_run=False)
    assert res[0]["action"] == "applied"
    assert executados, "aplicou comandos de verdade (no fake)"
    # a chave foi por stdin, nunca no argv
    assert all("SECRET" not in " ".join(argv) for argv, _ in executados)
    assert any(stdin == "SECRET" for _, stdin in executados)


def main():
    testes = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in testes:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(testes)} testes passaram")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"FALHOU: {e}")
        sys.exit(1)
