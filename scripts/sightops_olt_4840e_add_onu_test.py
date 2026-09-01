import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.cli.tools.olt_4840e_add_onu as mod

FALHAS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FALHAS.append(msg)


class FakeChannel:
    """Simula o canal SSH pro protocolo desta OLT: cada comando enviado
    troca o prompt conforme o contexto (config/pon/onu), igual ao real.
    `script(cmd, prompt) -> (reply_text, next_prompt)`."""

    def __init__(self, script, prompt="OLT_RADS#"):
        self._script = script
        self._prompt = prompt
        self._pending = ""
        self.commands: list[str] = []
        self.closed = False

    def recv_ready(self) -> bool:
        return bool(self._pending)

    def recv(self, n: int) -> bytes:
        out = self._pending[:n]
        self._pending = self._pending[n:]
        return out.encode()

    def send(self, data: str) -> int:
        cmd = data.rstrip("\n")
        self.commands.append(cmd)
        reply, self._prompt = self._script(cmd, self._prompt)
        self._pending += reply + "\n" + self._prompt
        return len(data)

    def close(self) -> None:
        self.closed = True


class FakeSSHClient:
    def __init__(self):
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _patch_open_shell(script, prompt="OLT_RADS#"):
    original = mod._open_shell
    def fake_open(*args, **kwargs):
        return FakeSSHClient(), FakeChannel(script, prompt)
    mod._open_shell = fake_open
    return original


def _patch_login(monkeypatch_noop=True):
    """`_ensure_logged_in`/`_ensure_enable` esperam textos especificos de
    login/senha que o FakeChannel nao precisa simular -- substitui as duas
    por no-ops nos testes (a autenticacao em si e testada nos scripts de
    `olt_4840e_collect_macs.py`, nao aqui)."""
    orig_login = mod._ensure_logged_in
    orig_enable = mod._ensure_enable
    mod._ensure_logged_in = lambda chan, user, password, timeout=12.0: None
    mod._ensure_enable = lambda chan, password, timeout=12.0: None
    return orig_login, orig_enable


def _unpatch(orig_open_shell, orig_login, orig_enable):
    mod._open_shell = orig_open_shell
    mod._ensure_logged_in = orig_login
    mod._ensure_enable = orig_enable


def _config_script(steps: dict, default_prompt="OLT_RADS(config)#"):
    """Helper pra testes: `steps` mapeia comando exato -> (reply, next_prompt).
    Comandos nao mapeados devolvem string vazia sem trocar o prompt."""
    def script(cmd, prompt):
        if cmd in steps:
            return steps[cmd]
        return "", prompt
    return script


# Confirmado ao vivo (equipamento real, OLT BARRA DE SAO MIGUEL): esta OLT
# pede confirmacao y/n pra 'copy running-config startup-config', igual ao
# 'onu-reboot'. Mesclar isso no dict de steps de um teste faz o cenario de
# salvar/confirmar batendo com o comportamento real -- sem isso, os testes
# ficariam esperando a confirmacao ate estourar o timeout.
_SAVE_CONFIRM_STEPS = {
    "copy running-config startup-config": ("Startup config in flash will be updated, are you sure(y/n)? [n]", "OLT_RADS#"),
    "y": ("\nBuilding, please wait...\nUpdate startup config successfully.", "OLT_RADS#"),
}


_STATUS_OUTPUT = (
    "ONU    Mac Address       Dis(m) RegisterTime      Type  Software   State\n"
    "0/4/6  30:e1:f1:73:a7:19 2654   26/07/29 06:09:43 other 1.3-220719 Up\n"
    "Total onu entries: 1 .\n"
    "onu online : 1 .\n"
)

_OPM_OUTPUT = (
    "ONU: 0/4/6\n"
    "Optical Transceiver Diagnosis :\n"
    "Work Temperature : 38.25 C\n"
    "Supply Voltage(Vcc) : 3.29 V\n"
    "TX Bias Current : 16.99 mA\n"
    "TX Power(Output) : 1.445 mW (3.00 dBm)\n"
    "RX Power(Input) : 0.573 mW (-2.40 dBm)\n"
)


def test_find_onu_4840e_finds_by_mac():
    def script(cmd, prompt):
        if cmd == "conf t":
            return "", "OLT_RADS(config)#"
        if cmd.startswith("show onu-status mac"):
            return _STATUS_OUTPUT, prompt
        return "", prompt

    orig_open_shell = _patch_open_shell(script)
    orig_login, orig_enable = _patch_login()
    try:
        result = mod.find_onu_4840e("100.64.10.5", "admin", "x", mac="30:e1:f1:73:a7:19")
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    check(result is not None, "esperava achar a ONU")
    check(result["pon"] == 4 and result["onu"] == 6, f"pon/onu errados: {result}")


def test_find_onu_4840e_returns_none_when_not_found():
    def script(cmd, prompt):
        if cmd == "conf t":
            return "", "OLT_RADS(config)#"
        if cmd.startswith("show onu-status mac"):
            return "Total onu entries: 0 .\n", prompt
        return "", prompt

    orig_open_shell = _patch_open_shell(script)
    orig_login, orig_enable = _patch_login()
    try:
        result = mod.find_onu_4840e("100.64.10.5", "admin", "x", mac="aa:bb:cc:dd:ee:ff")
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    check(result is None, f"esperava None, veio {result}")


_MAC_TABLE_ONU_OUTPUT = """MAC Address        VLAN   ONU      Status
aa:bb:cc:dd:ee:01  100    0/4/6    Active
aa:bb:cc:dd:ee:02  100    0/4/6    Active
Total: 2
"""


def test_onu_signal_4840e_combines_status_and_opm():
    def script(cmd, prompt):
        if cmd == "conf t":
            return "", "OLT_RADS(config)#"
        if cmd == "show onu-status":
            return _STATUS_OUTPUT, prompt
        if cmd == "onu 0/4/6":
            return "", "OLT_RADS(onu-0/4/6)#"
        if cmd == "show onu-opm-diagnosis":
            return _OPM_OUTPUT, prompt
        if cmd == "interface pon 0/4":
            return "", "OLT_RADS(config-if-pon-0/4)#"
        if cmd == "show mac-address-table onu 0/4/6":
            return _MAC_TABLE_ONU_OUTPUT, prompt
        if cmd == "exit":
            return "", "OLT_RADS(config)#"
        return "", prompt

    orig_open_shell = _patch_open_shell(script)
    orig_login, orig_enable = _patch_login()
    try:
        result = mod.onu_signal_4840e("100.64.10.5", "admin", "x", pon=4, onu=6)
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    check(result["ok"] is True, result)
    check(result["mac"] == "30:e1:f1:73:a7:19", f"mac errado: {result}")
    check(result["state"] == "Up", f"state errado: {result}")
    check(result["rx_power_dbm"] == -2.40, f"rx power errado: {result}")
    check(len(result.get("macs") or []) == 2, f"esperava 2 MACs atras da ONU: {result.get('macs')}")
    check(
        {m["mac"] for m in result["macs"]} == {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"},
        f"MACs errados: {result.get('macs')}",
    )


def test_connect_and_login_closes_on_ensure_logged_in_failure():
    """Verifica que _connect_and_login fecha a conexao SSH se _ensure_logged_in
    falhar DEPOIS da abertura do shell -- nenhum chamador recebe a referencia,
    entao ninguem conseguiria fechar sozinho."""
    def script(cmd, prompt):
        return "", prompt

    orig_open_shell = mod._open_shell
    orig_login = mod._ensure_logged_in
    orig_enable = mod._ensure_enable

    # Capturar as referencias do client/channel criados
    captured_client = None
    captured_chan = None

    def fake_open_shell(host, user, password, port=22, timeout=12.0):
        nonlocal captured_client, captured_chan
        captured_client = FakeSSHClient()
        captured_chan = FakeChannel(script)
        return captured_client, captured_chan

    # Patch _ensure_logged_in para lancar excecao
    mod._open_shell = fake_open_shell
    mod._ensure_logged_in = lambda chan, user, password, timeout=12.0: (
        (_ for _ in ()).throw(ValueError("credenciais invalidas"))
    )
    mod._ensure_enable = lambda chan, password, timeout=12.0: None

    try:
        exception_caught = False
        try:
            mod._connect_and_login("100.64.10.5", "admin", "bad_password", 22, 12.0)
        except ValueError as e:
            exception_caught = True
            check(str(e) == "credenciais invalidas", f"excecao nao propagou corretamente: {e}")

        check(exception_caught is True, "excecao nao foi lancada")
        check(captured_client is not None, "client nao foi criado")
        check(captured_chan is not None, "channel nao foi criado")
        check(captured_chan.closed is True, "channel nao foi fechado ao falhar login")
        check(captured_client.closed is True, "client nao foi fechado ao falhar login")
    finally:
        mod._open_shell = orig_open_shell
        mod._ensure_logged_in = orig_login
        mod._ensure_enable = orig_enable


def test_discover_onus_4840e_finds_unauthorized_mac():
    status_output = (
        "ONU    Mac Address       Dis(m) RegisterTime      Type  Software   State\n"
        "0/1/1  30:e1:f1:3e:a0:3f 2555   26/08/28 05:45:20 other 1.3-220719 Up\n"
        "0/1/2  00:0a:5a:ff:ff:69 -      -                 other -          Down\n"
        "0/1/3  30:e1:f1:3e:a0:99 -      -                 other -          Down\n"
    )
    white_output = (
        "WHITE LIST:\n"
        "Port Index Mac Address\n"
        "pon-0/1 1 30:e1:f1:3e:a0:3f\n"
        "pon-0/1 2 30:e1:f1:3e:a0:99\n"
        "Total white-list entries: 2 .\n"
    )

    def script(cmd, prompt):
        if cmd == "conf t":
            return "", "OLT_RADS(config)#"
        if cmd == "interface pon 0/1":
            return "", "OLT_RADS(config-if-pon-0/1)#"
        if cmd == "show onu-status":
            return status_output, prompt
        if cmd == "show white-list":
            return white_output, prompt
        if cmd == "exit":
            return "", "OLT_RADS(config)#"
        return "", prompt

    orig_open_shell = _patch_open_shell(script)
    orig_login, orig_enable = _patch_login()
    try:
        result = mod.discover_onus_4840e("100.64.10.5", "admin", "x", pon="1")
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    check(result["ok"] is True, result)
    candidates = result["pons"]["1"]["discovered"]
    check(len(candidates) == 1, f"esperava 1 candidata, veio {len(candidates)} ({candidates})")
    check(candidates[0]["mac"] == "00:0a:5a:ff:ff:69", f"mac errado: {candidates}")


def test_discover_onus_4840e_handles_invalid_pon():
    """Testa que o codigo falha gracefully quando interface pon nao existe"""
    commands_sent = []

    def script(cmd, prompt):
        commands_sent.append(cmd)
        if cmd == "conf t":
            return "", "OLT_RADS(config)#"
        if cmd == "interface pon 0/1":
            return "% Invalid parameter, error detected at '^' marker.", "OLT_RADS(config)#"
        if cmd == "exit":
            return "", "OLT_RADS(config)#"
        return "", prompt

    orig_open_shell = _patch_open_shell(script)
    orig_login, orig_enable = _patch_login()
    try:
        result = mod.discover_onus_4840e("100.64.10.5", "admin", "x", pon="1")
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    check(result["ok"] is True, result)
    pon_result = result["pons"]["1"]
    check("error" in pon_result, f"esperava 'error' na PON 1, veio {pon_result}")
    check("Falha ao entrar na PON 1" in pon_result["error"], f"error message errada: {pon_result['error']}")
    check(pon_result["discovered"] == [], f"esperava discovered vazio, veio {pon_result['discovered']}")
    # Verifica que show onu-status e show white-list nunca foram chamados
    check("show onu-status" not in commands_sent, "show onu-status foi chamado apos erro na interface")
    check("show white-list" not in commands_sent, "show white-list foi chamado apos erro na interface")


def test_add_onu_4840e_full_flow_success():
    white_list_after_add = (
        "WHITE LIST:\nPort Index Mac Address\npon-0/4 6 30:e1:f1:73:a7:19\nTotal white-list entries: 1 .\n"
    )
    steps = {
        "conf t": ("", "OLT_RADS(config)#"),
        "interface pon 0/4": ("", "OLT_RADS(config-if-pon-0/4)#"),
        "show onu-authenticate mode": ("pon 0/4 onu-authentication mode: disable", "OLT_RADS(config-if-pon-0/4)#"),
        "onu-authenticate mode mac-auth white-list": ("", "OLT_RADS(config-if-pon-0/4)#"),
        "white-list add mac 30:e1:f1:73:a7:19": ("", "OLT_RADS(config-if-pon-0/4)#"),
        "show white-list": (white_list_after_add, "OLT_RADS(config-if-pon-0/4)#"),
        "exit": ("", "OLT_RADS(config)#"),
        "onu 0/4/6": ("", "OLT_RADS(onu-0/4/6)#"),
        "onu-description Camera-Teste": ("", "OLT_RADS(onu-0/4/6)#"),
        "interface ethernet 0/1": ("", "OLT_RADS(eth-0/4/6/1)#"),
        "onu-vlan-mode tag vlan 3000": ("", "OLT_RADS(eth-0/4/6/1)#"),
        "onu-p2p": ("", "OLT_RADS(onu-0/4/6)#"),
        "end": ("", "OLT_RADS#"),
        **_SAVE_CONFIRM_STEPS,
    }

    orig_open_shell = _patch_open_shell(_config_script(steps))
    orig_login, orig_enable = _patch_login()
    try:
        result = mod.add_onu_4840e(
            "100.64.10.5", "admin", "x", pon=4, mac="30:e1:f1:73:a7:19",
            description="Camera-Teste", ports=[{"port": 1, "vlan": 3000}],
        )
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    check(result["ok"] is True, result)
    check(result["onu"] == 6, f"onu-id lido errado: {result}")
    check(result["saved"] is True, f"saved devia ser True: {result}")
    check("y" in result["commands_run"], f"devia ter confirmado o save com 'y' explicito: {result['commands_run']}")
    check("white-list add mac 30:e1:f1:73:a7:19" in result["commands_run"], result["commands_run"])
    check("onu-p2p" in result["commands_run"], result["commands_run"])


def test_add_onu_4840e_does_not_override_existing_loid_auth():
    steps = {
        "conf t": ("", "OLT_RADS(config)#"),
        "interface pon 0/4": ("", "OLT_RADS(config-if-pon-0/4)#"),
        "show onu-authenticate mode": ("pon 0/4 onu-authentication mode: loid-auth", "OLT_RADS(config-if-pon-0/4)#"),
    }

    orig_open_shell = _patch_open_shell(_config_script(steps))
    orig_login, orig_enable = _patch_login()
    try:
        try:
            mod.add_onu_4840e("100.64.10.5", "admin", "x", pon=4, mac="30:e1:f1:73:a7:19")
            check(False, "esperava OnuAddError")
        except mod.OnuAddError as e:
            check("loid-auth" in str(e), str(e))
            check("white-list add mac 30:e1:f1:73:a7:19" not in e.commands_run, e.commands_run)
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)


def test_add_onu_4840e_does_not_reset_auth_mode_when_already_mac_auth():
    steps = {
        "conf t": ("", "OLT_RADS(config)#"),
        "interface pon 0/4": ("", "OLT_RADS(config-if-pon-0/4)#"),
        "show onu-authenticate mode": ("pon 0/4 onu-authentication mode: mac-auth", "OLT_RADS(config-if-pon-0/4)#"),
        "white-list add mac 30:e1:f1:73:a7:19": ("", "OLT_RADS(config-if-pon-0/4)#"),
        "show white-list": ("WHITE LIST:\nPort Index Mac Address\npon-0/4 6 30:e1:f1:73:a7:19\nTotal white-list entries: 1 .\n", "OLT_RADS(config-if-pon-0/4)#"),
        "exit": ("", "OLT_RADS(config)#"),
        "onu 0/4/6": ("", "OLT_RADS(onu-0/4/6)#"),
        "onu-p2p": ("", "OLT_RADS(onu-0/4/6)#"),
        "end": ("", "OLT_RADS#"),
        **_SAVE_CONFIRM_STEPS,
    }

    orig_open_shell = _patch_open_shell(_config_script(steps))
    orig_login, orig_enable = _patch_login()
    try:
        result = mod.add_onu_4840e("100.64.10.5", "admin", "x", pon=4, mac="30:e1:f1:73:a7:19")
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    check(result["ok"] is True, result)
    check("onu-authenticate mode mac-auth white-list" not in result["commands_run"], result["commands_run"])


def test_add_onu_4840e_sanitizes_newline_in_description():
    """Um `description` com newline embutido nao pode virar um segundo
    comando enviado a OLT -- `_cli` manda `cmd.rstrip() + '\\n'`, que so tira
    espaco/newline do FIM da string, nao um newline no MEIO dela. Sem
    sanitizar, uma description tipo 'Cam\\nwhite-list add mac ...' colocaria
    esse segundo trecho na mesma linha de comando que a OLT real interpreta
    como um segundo ENTER. Prova que o driver sanitiza ANTES de montar o
    comando: o comando 'onu-description' realmente enviado (chan.commands)
    nao pode conter '\\n' embutido, e o texto injetado tem que aparecer
    como parte literal (inofensiva) da mesma linha, nao como comando
    separado."""
    malicious_description = "Camera-Teste\nwrite-list add mac aa:aa:aa:aa:aa:aa"
    white_list_after_add = (
        "WHITE LIST:\nPort Index Mac Address\npon-0/4 6 30:e1:f1:73:a7:19\nTotal white-list entries: 1 .\n"
    )
    steps = {
        "conf t": ("", "OLT_RADS(config)#"),
        "interface pon 0/4": ("", "OLT_RADS(config-if-pon-0/4)#"),
        "show onu-authenticate mode": ("pon 0/4 onu-authentication mode: disable", "OLT_RADS(config-if-pon-0/4)#"),
        "onu-authenticate mode mac-auth white-list": ("", "OLT_RADS(config-if-pon-0/4)#"),
        "white-list add mac 30:e1:f1:73:a7:19": ("", "OLT_RADS(config-if-pon-0/4)#"),
        "show white-list": (white_list_after_add, "OLT_RADS(config-if-pon-0/4)#"),
        "exit": ("", "OLT_RADS(config)#"),
        "onu 0/4/6": ("", "OLT_RADS(onu-0/4/6)#"),
        "onu-description Camera-Teste write-list add mac aa:aa:aa:aa:aa:aa": ("", "OLT_RADS(onu-0/4/6)#"),
        "onu-p2p": ("", "OLT_RADS(onu-0/4/6)#"),
        "end": ("", "OLT_RADS#"),
        **_SAVE_CONFIRM_STEPS,
    }

    orig_open_shell = _patch_open_shell(_config_script(steps))
    orig_login, orig_enable = _patch_login()
    try:
        result = mod.add_onu_4840e(
            "100.64.10.5", "admin", "x", pon=4, mac="30:e1:f1:73:a7:19",
            description=malicious_description,
        )
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    check(result["ok"] is True, result)
    desc_commands = [c for c in result["commands_run"] if c.startswith("onu-description")]
    check(len(desc_commands) == 1, f"esperava exatamente 1 comando onu-description, veio {desc_commands}")
    check("\n" not in desc_commands[0], f"onu-description nao pode conter newline embutido: {desc_commands[0]!r}")
    check(
        "write-list add mac aa:aa:aa:aa:aa:aa" in desc_commands[0],
        f"texto injetado devia virar parte literal (inofensiva) da description, nao um comando separado: {desc_commands[0]!r}",
    )


def test_add_onu_4840e_rejects_invalid_mac():
    """Um MAC que nao bate com o formato 'aa:bb:cc:dd:ee:ff' depois de
    normalizado (`_norm_mac` so normaliza separadores/caixa, nao valida
    formato) tem que falhar com ValueError ANTES de abrir qualquer conexao
    com a OLT -- senao ele vira parte literal de comandos como 'white-list
    add mac <mac>' / 'show onu-status mac <mac>'. Prova isso fazendo
    `_open_shell` explodir se for chamado -- se a validacao nao rodar antes
    da conexao, o teste falha por causa do AssertionError do fake, nao so
    por falta do ValueError esperado."""
    def fake_open_shell(*args, **kwargs):
        raise AssertionError("nao deveria abrir conexao com a OLT para um MAC invalido")

    orig_open_shell = mod._open_shell
    orig_login, orig_enable = _patch_login()
    mod._open_shell = fake_open_shell
    try:
        raised = False
        try:
            mod.add_onu_4840e("100.64.10.5", "admin", "x", pon=4, mac="not-a-mac")
        except ValueError as e:
            raised = True
            check("MAC invalido" in str(e), str(e))
        check(raised, "esperava ValueError para MAC invalido, nenhuma excecao foi lancada")
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)


def test_delete_onu_4840e_runs_both_steps_and_saves():
    steps = {
        "conf t": ("", "OLT_RADS(config)#"),
        "no onu-binding onu 0/4/6": ("", "OLT_RADS(config)#"),
        "interface pon 0/4": ("", "OLT_RADS(config-if-pon-0/4)#"),
        "white-list del mac 30:e1:f1:73:a7:19": ("", "OLT_RADS(config-if-pon-0/4)#"),
        "exit": ("", "OLT_RADS(config)#"),
        "end": ("", "OLT_RADS#"),
        **_SAVE_CONFIRM_STEPS,
    }

    orig_open_shell = _patch_open_shell(_config_script(steps))
    orig_login, orig_enable = _patch_login()
    try:
        result = mod.delete_onu_4840e("100.64.10.5", "admin", "x", pon=4, onu=6, mac="30:e1:f1:73:a7:19")
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    check(result["ok"] is True, result)
    check(result["saved"] is True, result)
    check("y" in result["commands_run"], f"devia ter confirmado o save com 'y' explicito: {result['commands_run']}")
    check("no onu-binding onu 0/4/6" in result["commands_run"], result["commands_run"])
    check("white-list del mac 30:e1:f1:73:a7:19" in result["commands_run"], result["commands_run"])


def test_delete_onu_4840e_reports_failure_when_whitelist_del_fails():
    steps = {
        "conf t": ("", "OLT_RADS(config)#"),
        "no onu-binding onu 0/4/6": ("", "OLT_RADS(config)#"),
        "interface pon 0/4": ("", "OLT_RADS(config-if-pon-0/4)#"),
        "white-list del mac 30:e1:f1:73:a7:19": ("% Invalid parameter, and error detected at '^' marker.", "OLT_RADS(config-if-pon-0/4)#"),
        "exit": ("", "OLT_RADS(config)#"),
        "end": ("", "OLT_RADS#"),
    }

    orig_open_shell = _patch_open_shell(_config_script(steps))
    orig_login, orig_enable = _patch_login()
    try:
        result = mod.delete_onu_4840e("100.64.10.5", "admin", "x", pon=4, onu=6, mac="30:e1:f1:73:a7:19")
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    check(result["ok"] is False, result)
    check("copy running-config startup-config" not in result["commands_run"], "nao devia salvar se falhou")


def test_delete_onu_4840e_still_attempts_whitelist_del_when_binding_fails():
    steps = {
        "conf t": ("", "OLT_RADS(config)#"),
        "no onu-binding onu 0/4/6": ("% Invalid parameter, and error detected at '^' marker.", "OLT_RADS(config)#"),
        "interface pon 0/4": ("", "OLT_RADS(config-if-pon-0/4)#"),
        "white-list del mac 30:e1:f1:73:a7:19": ("", "OLT_RADS(config-if-pon-0/4)#"),
        "exit": ("", "OLT_RADS(config)#"),
        "end": ("", "OLT_RADS#"),
    }

    orig_open_shell = _patch_open_shell(_config_script(steps))
    orig_login, orig_enable = _patch_login()
    try:
        result = mod.delete_onu_4840e("100.64.10.5", "admin", "x", pon=4, onu=6, mac="30:e1:f1:73:a7:19")
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    check(result["ok"] is False, result)
    check("white-list del mac 30:e1:f1:73:a7:19" in result["commands_run"], "white-list del devia ser tentado mesmo com onu-binding falhando")
    check("copy running-config startup-config" not in result["commands_run"], "nao devia salvar se falhou")


def test_reboot_onu_4840e_answers_confirmation_with_y():
    def script(cmd, prompt):
        if cmd == "conf t":
            return "", "OLT_RADS(config)#"
        if cmd == "onu 0/4/6":
            return "", "OLT_RADS(onu-0/4/6)#"
        # onu-reboot e tratado dentro do proprio teste via FakeChannel especial abaixo
        return "", prompt

    class ConfirmChannel(FakeChannel):
        """Canal especial so pra este teste: simula o prompt de confirmacao
        real da OLT quando recebe 'onu-reboot', e SO libera o reboot se
        receber 'y' como proximo envio -- se receber qualquer outra coisa
        (inclusive um comando novo, simulando o acidente real que
        aconteceu), marca `self.wrongly_confirmed = True`."""

        def __init__(self, script, prompt="OLT_RADS(onu-0/4/6)#"):
            super().__init__(script, prompt)
            self.awaiting_confirm = False
            self.wrongly_confirmed = False
            self.confirmed_with_y = False

        def send(self, data):
            cmd = data.rstrip("\n")
            self.commands.append(cmd)
            if self.awaiting_confirm:
                self.awaiting_confirm = False
                if cmd == "y":
                    self.confirmed_with_y = True
                    self._pending += "\n2016/11/07 17:58:13 EVENT (onu status): reboot ok\nOLT_RADS(onu-0/4/6)#"
                else:
                    self.wrongly_confirmed = True
                return len(data)
            if cmd == "onu-reboot":
                self.awaiting_confirm = True
                self._pending += "Are you sure you want to proceed with the system reboot(y/n)?[n]"
                return len(data)
            reply, self._prompt = self._script(cmd, self._prompt)
            self._pending += reply + "\n" + self._prompt
            return len(data)

    chan_holder = {}

    def fake_open_shell(host, user, password, port=22, timeout=12.0):
        chan = ConfirmChannel(script)
        chan_holder["chan"] = chan
        return FakeSSHClient(), chan

    orig_open_shell = mod._open_shell
    orig_login, orig_enable = _patch_login()
    mod._open_shell = fake_open_shell
    try:
        result = mod.reboot_onu_4840e("100.64.10.5", "admin", "x", pon=4, onu=6)
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    check(result["ok"] is True, result)
    check(chan_holder["chan"].confirmed_with_y is True, "esperava confirmar com 'y' explicito")
    check(chan_holder["chan"].wrongly_confirmed is False, "NUNCA deve confirmar com outra coisa que nao seja 'y'")


def test_reboot_onu_4840e_never_sends_bare_reboot():
    """Garante que nenhum comando REALMENTE ENVIADO a OLT e a string
    'reboot' isolada (sem o prefixo 'onu-') -- esse comando reinicia a OLT
    inteira, nao so a ONU. Verifica os comandos que passaram pelo canal
    (chan.commands), nao o texto-fonte do arquivo -- um scan estatico do
    codigo-fonte daria falso positivo, porque a palavra 'reboot' tambem
    aparece em prosa nos docstrings (ex: 'quase causou um reboot real da
    OLT' no docstring de _cli_confirm_reboot)."""
    def script(cmd, prompt):
        if cmd == "conf t":
            return "", "OLT_RADS(config)#"
        if cmd == "onu 0/4/6":
            return "", "OLT_RADS(onu-0/4/6)#"
        if cmd == "onu-reboot":
            return "Are you sure you want to proceed with the system reboot(y/n)?[n]y", "OLT_RADS(onu-0/4/6)#"
        return "", prompt

    chan_holder = {}

    def fake_open_shell(host, user, password, port=22, timeout=12.0):
        chan = FakeChannel(script, prompt="OLT_RADS(config)#")
        chan_holder["chan"] = chan
        return FakeSSHClient(), chan

    orig_open_shell = mod._open_shell
    orig_login, orig_enable = _patch_login()
    mod._open_shell = fake_open_shell
    try:
        mod.reboot_onu_4840e("100.64.10.5", "admin", "x", pon=4, onu=6)
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    sent_commands = chan_holder["chan"].commands
    check("reboot" not in sent_commands, f"NUNCA deve enviar o comando 'reboot' isolado -- comandos enviados: {sent_commands}")
    check("onu-reboot" in sent_commands, f"esperava 'onu-reboot' entre os comandos enviados: {sent_commands}")


def test_reboot_onu_4840e_never_answers_without_seeing_confirmation():
    """Prova que o guard do regex de confirmacao (_CONFIRM_YN_RE) e
    realmente obrigatorio pro resultado do teste: se a OLT responder
    'onu-reboot' com um prompt comum, SEM pedir confirmacao, o driver
    NUNCA manda 'y' por conta propria -- e reporta ok=False, nao um falso
    sucesso. Sem esse teste, remover o guard do regex nao quebraria
    nenhum teste (os outros dois so cobrem os casos onde a confirmacao
    aparece)."""
    def script(cmd, prompt):
        if cmd == "conf t":
            return "", "OLT_RADS(config)#"
        if cmd == "onu 0/4/6":
            return "", "OLT_RADS(onu-0/4/6)#"
        if cmd == "onu-reboot":
            # sem texto de confirmacao -- so devolve um prompt comum
            return "", "OLT_RADS(onu-0/4/6)#"
        return "", prompt

    chan_holder = {}

    def fake_open_shell(host, user, password, port=22, timeout=12.0):
        chan = FakeChannel(script, prompt="OLT_RADS(config)#")
        chan_holder["chan"] = chan
        return FakeSSHClient(), chan

    orig_open_shell = mod._open_shell
    orig_login, orig_enable = _patch_login()
    mod._open_shell = fake_open_shell
    try:
        result = mod.reboot_onu_4840e("100.64.10.5", "admin", "x", pon=4, onu=6, timeout=0.5)
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    sent_commands = chan_holder["chan"].commands
    check("y" not in sent_commands, f"NUNCA deve mandar 'y' sem ver o texto real de confirmacao -- comandos enviados: {sent_commands}")
    check(result["ok"] is False, f"sem confirmacao real, ok deve ser False (nao um falso sucesso): {result}")


def test_collect_onu_telemetry_4840e_maps_fields():
    status_output = (
        "ONU    Mac Address       Dis(m) RegisterTime      Type  Software   State\n"
        "0/1/1  30:e1:f1:3e:a0:3f 2555   26/08/28 05:45:20 other 1.3-220719 Up\n"
    )

    def script(cmd, prompt):
        if cmd == "conf t":
            return "", "OLT_RADS(config)#"
        if cmd == "interface pon 0/1":
            return "", "OLT_RADS(config-if-pon-0/1)#"
        if cmd == "show onu-status":
            return status_output, prompt
        if cmd == "exit":
            return "", "OLT_RADS(config)#"
        return "", prompt

    orig_open_shell = _patch_open_shell(script)
    orig_login, orig_enable = _patch_login()
    try:
        rows = mod.collect_onu_telemetry_4840e("100.64.10.5", "admin", "x", pon="1")
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    check(len(rows) == 1, f"esperava 1 linha, veio {rows}")
    row = rows[0]
    check(row["pon"] == 1 and row["onu_id"] == 1, f"pon/onu_id errados: {row}")
    check(row["serial"] == "30:e1:f1:3e:a0:3f", f"serial errado: {row}")
    check(row["oper_status"] == "Up", f"oper_status errado: {row}")
    check(row["distance_km"] == 2.555, f"distance_km errado: {row}")


def test_collect_onu_telemetry_4840e_skips_pon_with_interface_failure():
    """Se 'interface pon 0/<p>' falhar numa PON, a telemetria das outras
    PONs nao pode se perder -- pula so a que falhou. Prova isso olhando
    os comandos REALMENTE enviados ao canal: depois da falha em
    'interface pon 0/1', o proximo comando deve ser 'exit' (recuperacao),
    NUNCA 'show onu-status' -- se fosse, o guard nao estaria funcionando
    de verdade (um teste anterior que so checava o resultado final
    passava mesmo sem o guard, porque o filtro 'row["pon"] != p' escondia
    o problema)."""
    status_output_pon2 = (
        "ONU    Mac Address       Dis(m) RegisterTime      Type  Software   State\n"
        "0/2/1  30:e1:f1:3e:a0:a3 2896   26/08/28 05:45:19 other 1.3-220719 Up\n"
    )

    def script(cmd, prompt):
        if cmd == "conf t":
            return "", "OLT_RADS(config)#"
        if cmd == "interface pon 0/1":
            return "% Invalid parameter, and error detected at '^' marker.", "OLT_RADS(config)#"
        if cmd in ("interface pon 0/2", "interface pon 0/3", "interface pon 0/4"):
            return "", "OLT_RADS(config-if-pon)#"
        if cmd == "show onu-status":
            return status_output_pon2, prompt
        if cmd == "exit":
            return "", "OLT_RADS(config)#"
        return "", prompt

    chan_holder = {}

    def fake_open_shell(host, user, password, port=22, timeout=12.0):
        chan = FakeChannel(script, prompt="OLT_RADS(config)#")
        chan_holder["chan"] = chan
        return FakeSSHClient(), chan

    orig_open_shell = mod._open_shell
    orig_login, orig_enable = _patch_login()
    mod._open_shell = fake_open_shell
    try:
        rows = mod.collect_onu_telemetry_4840e("100.64.10.5", "admin", "x", pon="all")
    finally:
        _unpatch(orig_open_shell, orig_login, orig_enable)

    commands = chan_holder["chan"].commands
    idx_fail = commands.index("interface pon 0/1")
    check(
        commands[idx_fail + 1] == "exit",
        f"esperava 'exit' de recuperacao logo apos a falha em interface pon 0/1, "
        f"NUNCA 'show onu-status' -- comandos: {commands[idx_fail:idx_fail+3]}",
    )
    check(len(rows) >= 1, f"esperava telemetria das outras PONs preservada, veio {rows}")


def main() -> None:
    test_find_onu_4840e_finds_by_mac()
    test_find_onu_4840e_returns_none_when_not_found()
    test_onu_signal_4840e_combines_status_and_opm()
    test_connect_and_login_closes_on_ensure_logged_in_failure()
    test_discover_onus_4840e_finds_unauthorized_mac()
    test_discover_onus_4840e_handles_invalid_pon()
    test_add_onu_4840e_full_flow_success()
    test_add_onu_4840e_does_not_override_existing_loid_auth()
    test_add_onu_4840e_does_not_reset_auth_mode_when_already_mac_auth()
    test_add_onu_4840e_sanitizes_newline_in_description()
    test_add_onu_4840e_rejects_invalid_mac()
    test_delete_onu_4840e_runs_both_steps_and_saves()
    test_delete_onu_4840e_reports_failure_when_whitelist_del_fails()
    test_delete_onu_4840e_still_attempts_whitelist_del_when_binding_fails()
    test_reboot_onu_4840e_answers_confirmation_with_y()
    test_reboot_onu_4840e_never_sends_bare_reboot()
    test_reboot_onu_4840e_never_answers_without_seeing_confirmation()
    test_collect_onu_telemetry_4840e_maps_fields()
    test_collect_onu_telemetry_4840e_skips_pon_with_interface_failure()
    if FALHAS:
        print(f"FALHOU ({len(FALHAS)}):")
        for f in FALHAS:
            print(" -", f)
        raise SystemExit(1)
    print("OK: sightops_olt_4840e_add_onu_test")


if __name__ == "__main__":
    main()
