"""Controle de zoom/foco motorizado de cameras IP Intelbras (linha VIP-Z)
via protocolo JSON-RPC nativo (/RPC2), o mesmo usado pela interface web.

Descoberto por engenharia reversa em 2026-09-07 (camera VIP-3240-Z-G2,
firmware 2.680.00IB00E.0.T de 2021-07-07): a API HTTP CGI documentada
publicamente (devVideoInput.cgi?action=adjustFocus) NAO funciona nessa
familia de firmware -- sempre retorna 400 Bad Request, mesmo com a
sintaxe exata da doc oficial (HTTP_API_V3_59). A interface web real usa
um protocolo JSON-RPC com sessao (POST /RPC2 e /RPC2_Login), capturado
inspecionando a aba Rede do navegador enquanto o zoom era movido pela
tela. Ver docs/HANDOFF_AGENTES.md, entrada 2026-09-07 (LPR).

Diferenca importante de comportamento: os valores de focus/zoom no
comando real sao a posicao ABSOLUTA do motor (0.0 a 1.0), nao um delta
incremental como a doc do CGI antigo sugeria.

Sessao e vinculada a quem fez login (nao da pra reusar a sessao aberta
no navegador de outra maquina/IP -- retorna "Invalid session in request
data!") -- por isso este modulo sempre faz seu proprio login.
"""
from __future__ import annotations

import hashlib
import json

import requests


class CameraRPCError(RuntimeError):
    pass


def _md5up(texto: str) -> str:
    return hashlib.md5(texto.encode("utf-8")).hexdigest().upper()


class CameraZoomRPC:
    """Uma sessao de controle de zoom/foco contra uma camera Intelbras VIP-Z.

    Uso:
        cam = CameraZoomRPC("http://10.10.8.1", "admin", "senha")
        cam.login()
        cam.ajustar(zoom=0.3, focus=0.5)
        status = cam.status()
    """

    def __init__(self, base_url: str, usuario: str, senha: str, timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.usuario = usuario
        self.senha = senha
        self.timeout = timeout
        self.session: str | None = None
        self.object_id: int | None = None
        self._req_id = 0

    def _proximo_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _rpc(self, url_path: str, method: str, params=None, incluir_session: bool = True) -> dict:
        payload = {"method": method, "params": params, "id": self._proximo_id()}
        if incluir_session and self.session:
            payload["session"] = self.session
        if self.object_id is not None and method.startswith("devVideoInput."):
            payload["object"] = self.object_id
        resp = requests.post(
            f"{self.base_url}{url_path}",
            data=json.dumps(payload),
            timeout=self.timeout,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        resp.raise_for_status()
        return resp.json()

    def login(self, canal: int = 0) -> None:
        """Faz login (challenge + hash) e instancia o objeto devVideoInput do canal."""
        desafio = self._rpc("/RPC2_Login", "global.login", {
            "userName": self.usuario, "password": "", "clientType": "Web3.0",
            "ipAddr": "(null)", "loginType": "Direct",
        }, incluir_session=False)

        self.session = desafio.get("session")
        params = desafio.get("params") or {}
        realm, random_ = params.get("realm"), params.get("random")
        if not (self.session and realm and random_):
            raise CameraRPCError(f"Nao recebeu challenge de login esperado: {desafio}")

        pass1 = _md5up(f"{self.usuario}:{realm}:{self.senha}")
        pass_hash = _md5up(f"{self.usuario}:{random_}:{pass1}")

        login_real = self._rpc("/RPC2_Login", "global.login", {
            "userName": self.usuario, "password": pass_hash, "clientType": "Web3.0",
            "ipAddr": "(null)", "loginType": "Direct",
            "authorityType": params.get("encryption", "Default"), "passwordType": "Default",
        })
        if not login_real.get("result"):
            raise CameraRPCError(f"Login RPC2 falhou: {login_real}")
        self.session = login_real.get("session", self.session)

        instancia = self._rpc("/RPC2", "devVideoInput.factory.instance", {"channel": canal})
        obj = instancia.get("result")
        if not isinstance(obj, int):
            raise CameraRPCError(f"Nao conseguiu instanciar devVideoInput: {instancia}")
        self.object_id = obj

    def status(self) -> dict:
        """Retorna a posicao atual de zoom/foco (0.0-1.0 cada)."""
        resp = self._rpc("/RPC2", "devVideoInput.getFocusStatus", None)
        if not resp.get("result"):
            raise CameraRPCError(f"getFocusStatus falhou: {resp}")
        return resp["params"]["status"]

    def ajustar(self, zoom: float, focus: float) -> None:
        """Move o motor pra uma posicao ABSOLUTA (0.0 a 1.0 cada, nao incremento)."""
        resp = self._rpc("/RPC2", "devVideoInput.adjustFocus", {"focus": focus, "zoom": zoom})
        if not resp.get("result"):
            raise CameraRPCError(f"adjustFocus falhou: {resp}")


if __name__ == "__main__":
    import os
    import sys

    base = os.environ.get("CAM_URL", "http://127.0.0.1:18001")
    user = os.environ.get("CAM_USER", "admin")
    senha = os.environ.get("CAM_PASS", "")
    cam = CameraZoomRPC(base, user, senha)
    cam.login()
    print("status inicial:", cam.status())
    if len(sys.argv) > 2:
        cam.ajustar(zoom=float(sys.argv[1]), focus=float(sys.argv[2]))
        print("status apos ajuste:", cam.status())
