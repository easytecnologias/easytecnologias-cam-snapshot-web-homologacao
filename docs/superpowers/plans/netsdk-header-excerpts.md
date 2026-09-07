# Trechos do dhnetsdk.h / avglobal.h (NetSDK 3.050, Linux)

Extraído de dentro do WSL, de `~/netsdk/include/dhnetsdk.h` (copiado de
`NetSDK 3.050/Linux/General_NetSDK_Eng_Linux64_IS_V3.050.0000005.4.R.190306.tar.gz`).
Cruzado com o código-fonte real do demo oficial (`demo/03.PlayBack/dialog.cpp`)
pra confirmar o uso correto, não só a declaração.

## Tipos base (ramo Linux/`RELEASE_HEADER` do próprio dhnetsdk.h, não windows.h)

```c
#define WORD        unsigned short
#define DWORD       unsigned int
#define LONG        int
#define BOOL        int
#define BYTE        unsigned char
#define UINT        unsigned int
#define HWND        void*
#define LLONG       long
#define INT64       long long
#define LDWORD      long
#define CALL_METHOD   /* vazio no Linux -- sem stdcall */
#define CALLBACK      /* vazio no Linux -- sem stdcall */
#define CLIENT_NET_API  extern "C"
#define DH_SERIALNO_LEN  48
```

Tradução ctypes: `WORD→c_ushort`, `DWORD/UINT→c_uint`, `LONG/BOOL/int→c_int`,
`BYTE→c_ubyte`, `HWND→c_void_p`, `LLONG/LDWORD→c_long` (8 bytes em Linux
x86_64), `INT64→c_longlong`. Callbacks usam `ctypes.CFUNCTYPE` (convenção C
padrão, já que `CALLBACK`/`CALL_METHOD` são vazios no Linux).

## Login

Confirmado no código real do demo (`dialog.cpp:307`), não só na declaração:

```c
CLIENT_NET_API LLONG CALL_METHOD CLIENT_LoginEx2(
    const char *pchDVRIP, WORD wDVRPort,
    const char *pchUserName, const char *pchPassword,
    EM_LOGIN_SPAC_CAP_TYPE emSpecCap, void* pCapParam,
    LPNET_DEVICEINFO_Ex lpDeviceInfo, int *error = 0);

// uso real no demo:
// m_lLoginId = CLIENT_LoginEx2(ip, porta, usuario, senha,
//     EM_LOGIN_SPEC_CAP_TCP, NULL, &deviceInfo, &error);
// retorna 0 em caso de falha.

EM_LOGIN_SPEC_CAP_TCP = 0   // TCP login, default
```

```c
typedef struct {
    BYTE   sSerialNumber[48];     // DH_SERIALNO_LEN
    int    nAlarmInPortNum;
    int    nAlarmOutPortNum;
    int    nDiskNum;
    int    nDVRType;
    int    nChanNum;
    BYTE   byLimitLoginTime;
    BYTE   byLeftLogTimes;
    BYTE   bReserved[2];
    int    nLockLeftTime;
    char   Reserved[24];
} NET_DEVICEINFO_Ex, *LPNET_DEVICEINFO_Ex;
```

## NET_TIME

```c
typedef struct tagNET_TIME
{
    DWORD  dwYear;
    DWORD  dwMonth;
    DWORD  dwDay;
    DWORD  dwHour;
    DWORD  dwMinute;
    DWORD  dwSecond;
} NET_TIME, *LPNET_TIME;
```

## Playback por horário + callback de dados (CONFIRMADO no código real do demo)

```c
CLIENT_NET_API LLONG CALL_METHOD CLIENT_PlayBackByTimeEx2(
    LLONG lLoginID, int nChannelID,
    NET_IN_PLAY_BACK_BY_TIME_INFO *pstNetIn,
    NET_OUT_PLAY_BACK_BY_TIME_INFO *pstNetOut);

CLIENT_NET_API BOOL CALL_METHOD CLIENT_StopPlayBack(LLONG lPlayHandle);

CLIENT_NET_API BOOL CALL_METHOD CLIENT_SetPlayBackSpeed(LLONG lPlayHandle, EM_PLAY_BACK_SPEED emSpeed);

typedef struct tagNET_IN_PLAY_BACK_BY_TIME_INFO
{
    NET_TIME             stStartTime;
    NET_TIME             stStopTime;
    HWND                 hWnd;                  // NULL -- nao queremos renderizar
    fDownLoadPosCallBack cbDownLoadPos;          // callback de progresso (opcional)
    LDWORD               dwPosUser;
    fDataCallBack        fDownLoadDataCallBack;  // callback com o STREAM BRUTO
    LDWORD               dwDataUser;
    int                  nPlayDirection;         // 0 = normal, 1 = reverso
    int                  nWaittime;              // ex.: 5000 (ms)
    BYTE                 bReserved[1024];
} NET_IN_PLAY_BACK_BY_TIME_INFO;

typedef struct tagNET_OUT_PLAY_BACK_BY_TIME_INFO
{
    BYTE bReserved[1024];
} NET_OUT_PLAY_BACK_BY_TIME_INFO;

typedef int (CALLBACK *fDataCallBack)(LLONG lRealHandle, DWORD dwDataType, BYTE *pBuffer, DWORD dwBufSize, LDWORD dwUser);
typedef void (CALLBACK *fDownLoadPosCallBack)(LLONG lPlayHandle, DWORD dwTotalSize, DWORD dwDownLoadSize, LDWORD dwUser);

typedef enum tagEM_PLAY_BACK_SPEED
{
    EM_PLAY_BACK_SPEED_SLOW_16 = -4,
    EM_PLAY_BACK_SPEED_SLOW_8,
    EM_PLAY_BACK_SPEED_SLOW_4,
    EM_PLAY_BACK_SPEED_SLOW_2,
    EM_PLAY_BACK_SPEED_NORMAL = 0,
    EM_PLAY_BACK_SPEED_FAST_2,
    EM_PLAY_BACK_SPEED_FAST_4,
    EM_PLAY_BACK_SPEED_FAST_8,
    EM_PLAY_BACK_SPEED_FAST_16,
} EM_PLAY_BACK_SPEED;
```

## Achado importante: o callback de dados entrega stream BRUTO, não pixel

Confirmado no código real do demo (`dialog.cpp:25`, e o próprio demo escreve
os bytes recebidos direto num arquivo `.dav`): `fDownLoadDataCallBack`
entrega o stream codificado (H.264/H.265, mesmo formato que o projeto já
baixa via HTTP/`RPC_Loadfile`), não frames decodificados em pixel.
Decodificar é responsabilidade de quem recebe o callback -- a Task 4 do
plano de implementação canaliza esses bytes pra um processo `ffmpeg` via
pipe (o mesmo `ffmpeg` que o projeto inteiro já usa), em vez de integrar o
PlaySDK (uma segunda biblioteca nativa separada).
