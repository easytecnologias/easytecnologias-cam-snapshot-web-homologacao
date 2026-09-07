"""Bindings ctypes finos para o NetSDK Dahua/Intelbras (Linux).

So roda dentro do WSL/Linux -- nunca em producao. Nao guarda nenhuma
senha; tudo vem de parametro/variavel de ambiente na hora do uso.

Assinaturas confirmadas contra o header real (Linux) e o codigo-fonte do
demo oficial (demo/03.PlayBack/dialog.cpp) -- ver
docs/superpowers/plans/netsdk-header-excerpts.md.
"""
from __future__ import annotations

import ctypes
import os
from datetime import datetime
from typing import Callable

# Tipos base do dhnetsdk.h no ramo Linux (RELEASE_HEADER) -- WORD=c_ushort,
# DWORD/UINT=c_uint, LONG/BOOL/int=c_int, BYTE=c_ubyte, HWND=c_void_p,
# LLONG/LDWORD=c_long (8 bytes em x86_64), INT64=c_longlong.
LLONG = ctypes.c_long
DWORD = ctypes.c_uint
WORD = ctypes.c_ushort
LDWORD = ctypes.c_long
BYTE = ctypes.c_ubyte
BOOL = ctypes.c_int
HWND = ctypes.c_void_p

DH_SERIALNO_LEN = 48


class NetSDKError(RuntimeError):
    pass


class NET_TIME(ctypes.Structure):
    _fields_ = [
        ("dwYear", DWORD),
        ("dwMonth", DWORD),
        ("dwDay", DWORD),
        ("dwHour", DWORD),
        ("dwMinute", DWORD),
        ("dwSecond", DWORD),
    ]


def _net_time(dt: datetime) -> NET_TIME:
    return NET_TIME(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


class NET_DEVICEINFO_Ex(ctypes.Structure):
    _fields_ = [
        ("sSerialNumber", BYTE * DH_SERIALNO_LEN),
        ("nAlarmInPortNum", ctypes.c_int),
        ("nAlarmOutPortNum", ctypes.c_int),
        ("nDiskNum", ctypes.c_int),
        ("nDVRType", ctypes.c_int),
        ("nChanNum", ctypes.c_int),
        ("byLimitLoginTime", BYTE),
        ("byLeftLogTimes", BYTE),
        ("bReserved", BYTE * 2),
        ("nLockLeftTime", ctypes.c_int),
        ("Reserved", ctypes.c_char * 24),
    ]


EM_LOGIN_SPEC_CAP_TCP = 0

# fDataCallBack: int CALLBACK(LLONG lRealHandle, DWORD dwDataType, BYTE *pBuffer, DWORD dwBufSize, LDWORD dwUser)
FDATACALLBACK = ctypes.CFUNCTYPE(ctypes.c_int, LLONG, DWORD, ctypes.POINTER(BYTE), DWORD, LDWORD)
# fDownLoadPosCallBack: void CALLBACK(LLONG lPlayHandle, DWORD dwTotalSize, DWORD dwDownLoadSize, LDWORD dwUser)
FDOWNLOADPOSCALLBACK = ctypes.CFUNCTYPE(None, LLONG, DWORD, DWORD, LDWORD)


class NET_IN_PLAY_BACK_BY_TIME_INFO(ctypes.Structure):
    _fields_ = [
        ("stStartTime", NET_TIME),
        ("stStopTime", NET_TIME),
        ("hWnd", HWND),
        ("cbDownLoadPos", FDOWNLOADPOSCALLBACK),
        ("dwPosUser", LDWORD),
        ("fDownLoadDataCallBack", FDATACALLBACK),
        ("dwDataUser", LDWORD),
        ("nPlayDirection", ctypes.c_int),
        ("nWaittime", ctypes.c_int),
        ("bReserved", BYTE * 1024),
    ]


class NET_OUT_PLAY_BACK_BY_TIME_INFO(ctypes.Structure):
    _fields_ = [("bReserved", BYTE * 1024)]


EM_PLAY_BACK_SPEED = {
    -4: "SLOW_16", -3: "SLOW_8", -2: "SLOW_4", -1: "SLOW_2",
    0: "NORMAL",
    1: "FAST_2", 2: "FAST_4", 3: "FAST_8", 4: "FAST_16",
}

# Mantem referencia viva dos callbacks C enquanto a sessao estiver aberta --
# senao o Python coleta o objeto e o C chama um ponteiro morto.
_callbacks_ativos: list = []


def carregar_biblioteca(caminho_so: str | None = None) -> ctypes.CDLL:
    caminho_so = caminho_so or os.path.expanduser("~/netsdk/lib/libdhnetsdk.so")
    lib_dir = os.path.dirname(caminho_so)
    ld_path = os.environ.get("LD_LIBRARY_PATH", "")
    if lib_dir not in ld_path.split(":"):
        os.environ["LD_LIBRARY_PATH"] = f"{lib_dir}:{ld_path}" if ld_path else lib_dir
    return ctypes.CDLL(caminho_so)


def inicializar(lib: ctypes.CDLL) -> None:
    lib.CLIENT_Init.restype = ctypes.c_int
    lib.CLIENT_Init.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    ok = lib.CLIENT_Init(None, None)
    if not ok:
        raise NetSDKError("CLIENT_Init falhou")


def login(lib: ctypes.CDLL, host: str, porta: int, usuario: str, senha: str) -> int:
    lib.CLIENT_SetConnectTime.restype = None
    lib.CLIENT_SetConnectTime.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.CLIENT_SetConnectTime(10000, 2)

    lib.CLIENT_LoginEx2.restype = LLONG
    lib.CLIENT_LoginEx2.argtypes = [
        ctypes.c_char_p, WORD, ctypes.c_char_p, ctypes.c_char_p,
        ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(NET_DEVICEINFO_Ex), ctypes.POINTER(ctypes.c_int),
    ]
    device_info = NET_DEVICEINFO_Ex()
    erro = ctypes.c_int(0)
    handle = lib.CLIENT_LoginEx2(
        host.encode(), porta, usuario.encode(), senha.encode(),
        EM_LOGIN_SPEC_CAP_TCP, None, ctypes.byref(device_info), ctypes.byref(erro),
    )
    if handle == 0:
        raise NetSDKError(f"CLIENT_LoginEx2 falhou, codigo de erro {erro.value}")
    return handle


def logout(lib: ctypes.CDLL, handle_login: int) -> None:
    lib.CLIENT_Logout.restype = BOOL
    lib.CLIENT_Logout.argtypes = [LLONG]
    lib.CLIENT_Logout(handle_login)


def abrir_playback(
    lib: ctypes.CDLL,
    handle_login: int,
    canal: int,
    inicio: datetime,
    fim: datetime,
    on_bytes_brutos: Callable[[bytes], None],
) -> int:
    """`canal` e indice 0-based (canal "4" que o usuario ve no DVR = canal=3
    aqui). Confirmado ao vivo: o mesmo offset que ja existia na API HTTP
    (a resposta do mediaFileFind tambem devolve o Channel 0-based, embora
    o parametro de busca condition.Channel seja 1-based -- inconsistencia
    do proprio fabricante, nao nossa. NetSDK e 0-based dos dois lados."""
    lib.CLIENT_PlayBackByTimeEx2.restype = LLONG
    lib.CLIENT_PlayBackByTimeEx2.argtypes = [
        LLONG, ctypes.c_int,
        ctypes.POINTER(NET_IN_PLAY_BACK_BY_TIME_INFO), ctypes.POINTER(NET_OUT_PLAY_BACK_BY_TIME_INFO),
    ]

    def _callback_dados(l_real_handle, dw_data_type, p_buffer, dw_buf_size, dw_user):
        if dw_buf_size > 0:
            dados = ctypes.string_at(p_buffer, dw_buf_size)
            on_bytes_brutos(dados)
        return 1

    cb_dados = FDATACALLBACK(_callback_dados)
    cb_pos = FDOWNLOADPOSCALLBACK(lambda *args: None)
    _callbacks_ativos.append(cb_dados)
    _callbacks_ativos.append(cb_pos)

    net_in = NET_IN_PLAY_BACK_BY_TIME_INFO()
    net_in.stStartTime = _net_time(inicio)
    net_in.stStopTime = _net_time(fim)
    net_in.hWnd = None
    net_in.cbDownLoadPos = cb_pos
    net_in.dwPosUser = 0
    net_in.fDownLoadDataCallBack = cb_dados
    net_in.dwDataUser = 0
    net_in.nPlayDirection = 0
    net_in.nWaittime = 5000

    net_out = NET_OUT_PLAY_BACK_BY_TIME_INFO()

    handle_play = lib.CLIENT_PlayBackByTimeEx2(handle_login, canal, ctypes.byref(net_in), ctypes.byref(net_out))
    if handle_play == 0:
        _callbacks_ativos.remove(cb_dados)
        _callbacks_ativos.remove(cb_pos)
        raise NetSDKError("CLIENT_PlayBackByTimeEx2 falhou")
    return handle_play


def set_velocidade(lib: ctypes.CDLL, handle_playback: int, velocidade: int) -> None:
    if velocidade not in EM_PLAY_BACK_SPEED:
        raise ValueError(f"velocidade {velocidade} invalida, use -4 a 4")
    lib.CLIENT_SetPlayBackSpeed.restype = BOOL
    lib.CLIENT_SetPlayBackSpeed.argtypes = [LLONG, ctypes.c_int]
    ok = lib.CLIENT_SetPlayBackSpeed(handle_playback, velocidade)
    if not ok:
        raise NetSDKError(f"CLIENT_SetPlayBackSpeed({velocidade}) falhou")


def fechar_playback(lib: ctypes.CDLL, handle_playback: int) -> None:
    lib.CLIENT_StopPlayBack.restype = BOOL
    lib.CLIENT_StopPlayBack.argtypes = [LLONG]
    lib.CLIENT_StopPlayBack(handle_playback)
    _callbacks_ativos.clear()
