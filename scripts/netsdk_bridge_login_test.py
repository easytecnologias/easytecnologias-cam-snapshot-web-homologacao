"""Teste manual: login real contra um DVR. Nao roda em CI, roda na mao.

Uso:
  NETSDK_HOST=127.0.0.1 NETSDK_PORT=37777 NETSDK_USER=admin NETSDK_PASS='...' \
    python3 scripts/netsdk_bridge_login_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts import netsdk_bridge

host = os.environ["NETSDK_HOST"]
porta = int(os.environ.get("NETSDK_PORT", "37777"))
usuario = os.environ["NETSDK_USER"]
senha = os.environ["NETSDK_PASS"]

lib = netsdk_bridge.carregar_biblioteca()
netsdk_bridge.inicializar(lib)
handle = netsdk_bridge.login(lib, host, porta, usuario, senha)
print("login OK, handle:", handle)
netsdk_bridge.logout(lib, handle)
print("logout OK")
