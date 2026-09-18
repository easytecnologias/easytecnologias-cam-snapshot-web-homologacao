
import requests, os, re, json
from requests.auth import HTTPDigestAuth
from .model_fallback import best_guess_model


HEADERS = {"User-Agent": "cam-snapshot/1.7.4"}

def _split_ipv4_host_port(value: str):
    v = (value or "").strip()
    m = re.match(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})$", v)
    if not m:
        return v, None
    try:
        return m.group(1), int(m.group(2))
    except Exception:
        return m.group(1), None

def _base_http_url(ip_or_host: str, port: int | None = None, scheme: str = "http") -> str:
    host, emb_port = _split_ipv4_host_port(ip_or_host)
    p = emb_port if emb_port is not None else port
    if p is None:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{int(p)}"



def _normalize_title(s: str | None) -> str | None:
    if not s:
        return None
    # remove espaÃ§os extras e deixa tudo MAIÃšSCULO
    s = " ".join(str(s).split())
    return s.upper()

MAC_RE = re.compile(r"([0-9A-Fa-f]{2}([-:])){5}[0-9A-Fa-f]{2}")
MAC_HEX12_RE = re.compile(r"\b[0-9A-Fa-f]{12}\b")
BRAND_MAP = {
    "intelbras": "Intelbras",
    "hikvision": "Hikvision",
    "hilook": "HiLook",
    "dahua": "Dahua",
    "hua": "Dahua",
    "axis": "Axis",
    "tp-link": "TP-Link",
    "tplink": "TP-Link",
    "unv": "UNV",
    "uniview": "UNV",
}

def _clean_brand(s: str):
    if not s:
        return None
    s = s.strip().strip("\"'")
    s = re.sub(r"[^A-Za-z0-9\- ]+", "", s)
    s_low = s.lower()
    for k, v in BRAND_MAP.items():
        if k in s_low:
            return v
    return s.title() if s else None

def _brand_from_model(model: str | None) -> str | None:
    if not model:
        return None
    m = model.upper()
    if m.startswith(("VIP", "MIB", "VHD", "MHD")):
        return "Intelbras"
    if m.startswith(("DHI", "DH-", "HFW", "HDP", "IPC-H", "IPC-D")):
        return "Dahua"
    if m.startswith(("DS-", "HWI", "HWP", "HK")):
        return "Hikvision"
    if "HILOOK" in m:
        return "HiLook"
    return None

def _canonicalize_brand_from_model(info: dict | None) -> dict:
    out = info if isinstance(info, dict) else {}
    model_brand = _brand_from_model(out.get("modelo") or out.get("model"))
    if model_brand:
        out["fabricante"] = model_brand
    return out

def _extract_brand_from_text(txt: str) -> str | None:
    if not txt:
        return None
    for line in txt.splitlines():
        low = line.lower()
        if any(k in low for k in ("brand", "vendor", "manufacturer", "oem")):
            if "=" in line:
                val = line.split("=", 1)[1]
            else:
                val = line.split(":", 1)[-1]
            brand = _clean_brand(val)
            if brand:
                return brand
    return None

def _normalize_mac(raw: str | None) -> str | None:
    s = (raw or "").strip().lower().replace("-", "").replace(":", "")
    if len(s) != 12 or not re.fullmatch(r"[0-9a-f]{12}", s):
        return None
    return ":".join(s[i:i+2] for i in range(0, 12, 2))

def _try_get(url, user, password, timeout, use_digest=False, stream=False):
    auth = HTTPDigestAuth(user, password) if use_digest else (user, password)
    try:
        return requests.get(url, auth=auth, timeout=timeout, headers=HEADERS, stream=stream)
    except Exception:
        return None


def _auth_fast_check(ip: str, user: str, password: str, timeout=(0.8, 1.5)) -> dict:
    """Detecta rapidamente credenciais rejeitadas.

    ObservaÃ§Ã£o importante (Intelbras VIP / Dahua OEM): o endpoint "/" pode retornar 200
    mesmo com usuÃ¡rio/senha errados. Para detectar erro de credenciais, usamos endpoints
    realmente protegidos, como o snapshot CGI.

    Retorna:
      {
        "auth_failed": bool,
        "status_code": int|None,
        "url": str|None,
      }
    """

    ip = (ip or "").strip()
    if not ip:
        return {"auth_failed": False, "status_code": None, "url": None}

    # Endpoints protegidos mais comuns:
    # - Intelbras/Dahua: snapshot.cgi
    # - UNV/Uniview: LAPI DeviceInfo
    candidates = [
        (_base_http_url(ip) + "/cgi-bin/snapshot.cgi"),
        (_base_http_url(ip) + "/cgi-bin/snapshot.cgi?channel=1"),
        (_base_http_url(ip) + "/LAPI/V1.0/System/DeviceInfo"),
    ]

    for url in candidates:
        # usa digest (Ã© o que a Intelbras VIP 3230-B costuma pedir)
        r = _try_get(url, user, password, timeout=timeout, use_digest=True, stream=True)
        if r is None:
            continue
        code = getattr(r, "status_code", None)
        if code in (401, 403):
            return {"auth_failed": True, "status_code": int(code), "url": url}
        if code == 200:
            return {"auth_failed": False, "status_code": int(code), "url": url}
        # 404/405/... -> nÃ£o confirma auth (segue tentando outros candidatos)

    return {"auth_failed": False, "status_code": None, "url": None}


def _probe_unv_lapi(ip, user, password, timeout):
    """Tenta obter infos de cÃ¢meras UNV/Uniview via LAPI.
    Retorna dict parcial com modelo/fabricante/serial/firmware/titulo ou None.
    """
    url = _base_http_url(ip) + "/LAPI/V1.0/System/DeviceInfo"
    # UNV costuma aceitar Digest.
    for use_digest in (True, False):
        r = _try_get(url, user, password, timeout, use_digest=use_digest, stream=False)
        if r is None or r.status_code != 200 or not getattr(r, "text", ""):
            continue
        try:
            obj = r.json()
        except Exception:
            try:
                obj = json.loads((r.text or "").replace("\ufeff", ""))
            except Exception:
                obj = None
        if not isinstance(obj, dict):
            continue

        resp = obj.get("Response") if isinstance(obj.get("Response"), dict) else {}
        data = resp.get("Data") if isinstance(resp.get("Data"), dict) else {}
        if not data:
            continue

        model = str(data.get("DeviceModel") or data.get("PrototypeName") or data.get("DeviceName") or "").strip()
        serial = str(data.get("SerialNumber") or "").strip()
        firmware = str(data.get("FirmwareVersion") or "").strip()
        name = str(data.get("DeviceName") or "").strip()

        out: dict[str, str] = {"fabricante": "UNV"}
        if model:
            out["modelo"] = model
        if serial:
            out["serial"] = serial
        if firmware:
            out["firmware"] = firmware

        # Titulo "verdadeiro" do overlay da camera (ex.: "66 - QD D e G")
        # costuma estar em /LAPI/V1.0/Channel/0/Media/OSD (InfoType=1).
        title_val = ""
        try:
            osd_url = _base_http_url(ip) + "/LAPI/V1.0/Channel/0/Media/OSD"
            r_osd = _try_get(osd_url, user, password, timeout, use_digest=use_digest, stream=False)
            if r_osd is not None and r_osd.status_code == 200 and getattr(r_osd, "text", ""):
                try:
                    osd_obj = r_osd.json()
                except Exception:
                    try:
                        osd_obj = json.loads((r_osd.text or "").replace("\ufeff", ""))
                    except Exception:
                        osd_obj = {}
                if isinstance(osd_obj, dict):
                    osd_resp = osd_obj.get("Response") if isinstance(osd_obj.get("Response"), dict) else {}
                    osd_data = osd_resp.get("Data") if isinstance(osd_resp.get("Data"), dict) else {}
                    info_osd = osd_data.get("InfoOSD") if isinstance(osd_data.get("InfoOSD"), list) else []
                    for area in info_osd:
                        if not isinstance(area, dict):
                            continue
                        params = area.get("InfoParam") if isinstance(area.get("InfoParam"), list) else []
                        for p in params:
                            if not isinstance(p, dict):
                                continue
                            if str(p.get("InfoType")) == "1":
                                v = str(p.get("Value") or "").strip()
                                if v:
                                    title_val = v
                                    break
                        if title_val:
                            break
        except Exception:
            title_val = ""

        if title_val:
            out["titulo"] = _normalize_title(title_val)
        elif name:
            out["titulo"] = _normalize_title(name)
        return out
    return None

def _get_text(urls, user, password, timeout, retries=0):
    for url in urls:
        for _ in range(retries + 1):
            r = _try_get(url, user, password, timeout, stream=False)
            if r is not None and r.status_code == 200 and r.text:
                return r.text
            r = _try_get(url, user, password, timeout, use_digest=True, stream=False)
            if r is not None and r.status_code == 200 and r.text:
                return r.text
    return None




def _probe_channel_title_raw(ip, user, password, timeout):
    """Tenta obter o tÃ­tulo via CGI ChannelTitle direto.

    Ãštil para firmwares Dahua/Intelbras onde o ChannelTitle responde,
    mas o fluxo padrÃ£o nÃ£o conseguiu preencher `info["titulo"]`.
    """
    try:
        import requests
        from requests.auth import HTTPDigestAuth
    except Exception:
        return None

    url = _base_http_url(ip) + "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle"

    # tenta acesso sem auth, depois Basic e por fim Digest
    auth_chain = [None, (user, password), HTTPDigestAuth(user, password)]
    for auth in auth_chain:
        try:
            if auth is None:
                r = requests.get(url, timeout=timeout)
            else:
                r = requests.get(url, auth=auth, timeout=timeout)
        except Exception:
            continue

        if r.status_code != 200 or not getattr(r, "text", ""):
            continue

        for line in r.text.splitlines():
            line = line.strip()
            if ".Name=" in line:
                raw = line.split("=", 1)[1].strip()
                # preserva o titulo completo retornado pela camera
                # (ex.: "65 - QUADRA F E H 2"), apenas normalizando espacos/case
                raw = " ".join(raw.split()).upper()
                return raw or None

    return None





def _xml_first_text_by_tag_suffix(root, wanted_suffixes):
    """Namespace-safe XML search.

    Hikvision/HiLook retornam XML com namespace (ex: {uri}name).
    Esta funÃ§Ã£o procura pelo primeiro elemento cujo tag termine com um dos sufixos
    desejados e retorne texto nÃ£o-vazio.
    """
    wanted_suffixes = tuple(str(s).lower() for s in (wanted_suffixes or ()))
    # 1) preferir tags exatamente "name" (com ou sem namespace)
    for elem in root.iter():
        tag = getattr(elem, 'tag', '')
        if not isinstance(tag, str):
            continue
        low = tag.lower()
        # pega o nome do elemento sem namespace
        local = low.split('}', 1)[-1] if '}' in low else low
        if local in wanted_suffixes and elem.text and str(elem.text).strip():
            return str(elem.text).strip()
    # 2) fallback: qualquer tag que termine com o sufixo
    for elem in root.iter():
        tag = getattr(elem, 'tag', '')
        if not isinstance(tag, str):
            continue
        low = tag.lower()
        if any(low.endswith(suf) for suf in wanted_suffixes) and elem.text and str(elem.text).strip():
            return str(elem.text).strip()
    return None


def _json_first_name(obj):
    """Procura recursivamente por um campo de nome em JSON (ISAPI com format=json)."""
    if isinstance(obj, dict):
        # preferÃªncias
        for k in ("name", "channelName", "deviceName"):
            v = obj.get(k)
            if isinstance(v, (str, int)) and str(v).strip():
                return str(v).strip()
        # objetos conhecidos
        for k in ("VideoInputChannel", "VideoInputChannelList", "InputProxyChannel", "StreamingChannel", "Channel"):
            v = obj.get(k)
            if isinstance(v, dict):
                got = _json_first_name(v)
                if got:
                    return got
            if isinstance(v, list):
                for it in v:
                    got = _json_first_name(it)
                    if got:
                        return got
        # busca genÃ©rica
        for k, v in obj.items():
            if isinstance(v, (str, int)) and str(k).lower().endswith('name') and str(v).strip():
                return str(v).strip()
            got = _json_first_name(v)
            if got:
                return got
    elif isinstance(obj, list):
        for it in obj:
            got = _json_first_name(it)
            if got:
                return got
    return None


def _json_first_channel_name(obj):
    """Procura nome de canal em JSON ISAPI, evitando deviceName genÃ©rico."""
    if isinstance(obj, dict):
        for k in ("channelName", "name"):
            v = obj.get(k)
            if isinstance(v, (str, int)) and str(v).strip():
                return str(v).strip()
        for k in ("VideoInputChannel", "VideoInputChannelList", "InputProxyChannel", "StreamingChannel", "Channel"):
            v = obj.get(k)
            if isinstance(v, dict):
                got = _json_first_channel_name(v)
                if got:
                    return got
            if isinstance(v, list):
                for it in v:
                    got = _json_first_channel_name(it)
                    if got:
                        return got
        for k, v in obj.items():
            kl = str(k).lower()
            if isinstance(v, (str, int)) and str(v).strip():
                if kl in ("channelname", "name") or ("channel" in kl and kl.endswith("name")):
                    return str(v).strip()
            got = _json_first_channel_name(v)
            if got:
                return got
    elif isinstance(obj, list):
        for it in obj:
            got = _json_first_channel_name(it)
            if got:
                return got
    return None


def _xml_first_channel_name(root):
    """Procura nome de canal em XML ISAPI, evitando deviceName."""
    for elem in root.iter():
        tag = getattr(elem, "tag", "")
        if not isinstance(tag, str):
            continue
        low = tag.lower()
        local = low.split("}", 1)[-1] if "}" in low else low
        if local in ("channelname", "name"):
            txt = (elem.text or "").strip()
            if txt:
                return txt
    return None


def _probe_hikvision_channel_title(ip, user, password, timeout):
    """ObtÃ©m o nome do canal em Hikvision/HiLook via ISAPI.

    Ex:
      /ISAPI/System/Video/inputs/channels
      /ISAPI/System/Video/inputs/channels/1

    Retorna string (nome) ou None.
    """
    endpoints = [
        f"http://{ip}/ISAPI/System/Video/inputs/channels?format=json",
        f"http://{ip}/ISAPI/System/Video/inputs/channels",
        f"http://{ip}/ISAPI/System/Video/inputs/channels/1?format=json",
        f"http://{ip}/ISAPI/System/Video/inputs/channels/1",
        f"http://{ip}/ISAPI/ContentMgmt/InputProxy/channels/1?format=json",
        f"http://{ip}/ISAPI/ContentMgmt/InputProxy/channels/1",
        f"http://{ip}/ISAPI/Streaming/channels/101?format=json",
        f"http://{ip}/ISAPI/Streaming/channels/101",
    ]

    for url in endpoints:
        for use_digest in (True, False):
            r = _try_get(url, user, password, timeout, use_digest=use_digest, stream=False)
            if r is None or r.status_code != 200:
                continue

            ctype = (r.headers.get('Content-Type') or '').lower()
            body = getattr(r, 'text', None) or ''

            # JSON
            if 'json' in ctype or body.strip().startswith('{'):
                try:
                    data = r.json()
                except Exception:
                    data = None
                got = _json_first_channel_name(data) if data is not None else None
                if got:
                    return got

            # XML
            if body:
                try:
                    from xml.etree import ElementTree as ET
                    root = ET.fromstring(body.encode('utf-8') if isinstance(body, str) else body)
                    got = _xml_first_channel_name(root)
                    if got:
                        return got
                except Exception:
                    pass

    return None

def _probe_hikvision_isapi(ip, user, password, timeout):
    """Tenta obter informaÃ§Ãµes via ISAPI (Hikvision/HiLook).
    Retorna dict parcial com modelo/fabricante/serial/firmware ou None.
    """
    url = f"http://{ip}/ISAPI/System/deviceInfo"
    for use_digest in (False, True):
        r = _try_get(url, user, password, timeout, use_digest=use_digest, stream=False)
        if r is None or r.status_code != 200 or not r.text:
            continue
        try:
            from xml.etree import ElementTree as ET
            root = ET.fromstring(r.text.encode("utf-8") if isinstance(r.text, str) else r.text)
        except Exception:
            return None

        def _find_tag(name: str):
            for elem in root.iter():
                tag = getattr(elem, "tag", "")
                if isinstance(tag, str) and tag.lower().endswith(name.lower()):
                    if elem.text:
                        return elem.text.strip()
            return None

        model = _find_tag("model") or _find_tag("deviceType")
        serial = _find_tag("serialNumber") or _find_tag("deviceSerialNo")
        firmware = _find_tag("firmwareVersion") or _find_tag("swVersion")
        manuf = _find_tag("manufacturer") or _find_tag("devManufacturer")
        mac = _find_tag("macAddress") or _find_tag("mac") or _find_tag("macAddr")
        name = _find_tag("deviceName") or _find_tag("name")

        out: dict[str, str] = {}
        # campos Ãºteis via ISAPI
        if model:
            out["modelo"] = model
        if manuf:
            out["fabricante"] = _clean_brand(manuf)
        if serial:
            out["serial"] = serial
        if mac:
            out["mac"] = mac
        if name:
            out["titulo"] = _normalize_title(name)
        if firmware:
            out["firmware"] = firmware

        if out:
            if "fabricante" not in out:
                out["fabricante"] = "Hikvision"
            return out
    return None


def _enrich_from_html_root(ip, user, password, info: dict, timeout):
    """Se modelo/fabricante vierem vazios, tenta extrair da pÃ¡gina HTML raiz.
    Focado em Intelbras e HiLook, usando apenas GET em http://ip/.
    """
    if info is None:
        info = {}
    if info.get("modelo") and info.get("fabricante"):
        return info

    url = f"http://{ip}/"
    for use_digest in (False, True):
        r = _try_get(url, user, password, timeout, use_digest=use_digest, stream=False)
        if r is None or r.status_code != 200 or not r.text:
            continue
        html = r.text
        low = html.lower()

        # Intelbras
        if "intelbras" in low:
            if not info.get("fabricante"):
                info["fabricante"] = "Intelbras"
            if not info.get("modelo"):
                m = re.search(r"vip[-\s]?[0-9a-zA-Z\-]{2,}", html, re.IGNORECASE)
                if m:
                    info["modelo"] = m.group(0).replace("  ", " ").strip()

        # HiLook
        if "hilook" in low or "hi-look" in low:
            if not info.get("fabricante"):
                info["fabricante"] = "HiLook"
            if not info.get("modelo"):
                m = re.search(r"(DS-|IPC-)[0-9A-Z\-]+", html, re.IGNORECASE)
                if m:
                    info["modelo"] = m.group(0).upper()

        # UNV / Uniview
        if "uniview" in low or " unv" in low or "unv " in low:
            if not info.get("fabricante"):
                info["fabricante"] = "UNV"
            if not info.get("modelo"):
                m = re.search(r"(IPC|NVR)[0-9A-Z\-]{4,}", html, re.IGNORECASE)
                if m:
                    info["modelo"] = m.group(0).upper()
        break

    return info


def probe_device(ip, user, password, timeout=(1.2, 2.5), retries=1):
    """Coleta modelo/fabricante/tÃ­tulo da cÃ¢mera usando mÃºltiplas estratÃ©gias.

    1) CGIs Dahua/Intelbras (magicBox/configManager).
    2) ISAPI (Hikvision/HiLook).
    3) PÃ¡gina HTML raiz (Intelbras/HiLook).
    4) Fallback heurÃ­stico (best_guess_model).
    """
    info: dict = {"ip": ip, "modelo": None, "fabricante": None, "titulo": None, "mac": None}

    # ------------------------------------------------------------
    # AUTH fast-check (principalmente Intelbras/Dahua/Hik/HiLook)
    # Nota: a pÃ¡gina / pode retornar 200 mesmo com senha errada; por isso
    # testamos um endpoint realmente protegido.
    # ------------------------------------------------------------
    chk = _auth_fast_check(ip, user, password, timeout=(0.8, 1.5))
    if chk.get("auth_failed") is True:
        info["status"] = "auth_failed"
        info["auth_url"] = chk.get("url") or ""
        info["auth_status"] = chk.get("status")
        info["auth_method"] = chk.get("method") or ""
        # retorna cedo para o scan pular rÃ¡pido
        return info

    # 1) CGIs padrÃ£o Dahua/Intelbras
    model_urls = [
        f"http://{ip}/cgi-bin/magicBox.cgi?action=getDeviceType",
        f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=DeviceInfo",
        f"http://{ip}/cgi-bin/magicBox.cgi?action=getSystemInfo",
    ]
    txt_model = _get_text(model_urls, user, password, timeout, retries)
    if txt_model:
        info["fabricante"] = _extract_brand_from_text(txt_model)
        for line in txt_model.splitlines():
            line = line.strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            key = str(k or "").strip().lower()
            val = str(v or "").strip()
            if not val:
                continue
            # Alguns Intelbras antigos respondem getDeviceType como "type=VIP-3220-B"
            if key in ("devicetype", "model", "type", "producttype", "machinemodel"):
                info["modelo"] = val
                break
        if info.get("modelo") and not info.get("fabricante"):
            info["fabricante"] = _brand_from_model(info.get("modelo"))

    # 2) TÃ­tulos dos canais (ChannelTitle)
    title_urls = [
        f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle",
        f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=All",
    ]
    txt_title = _get_text(title_urls, user, password, timeout, retries)
    if txt_title:
        for line in txt_title.splitlines():
            line = line.strip()
            if "ChannelTitle" in line and "Name=" in line:
                raw_title = line.split("Name=", 1)[1].strip()
                info["titulo"] = _normalize_title(raw_title)
                break


    # 2b) Fallback extra: se ainda nÃ£o tiver tÃ­tulo, forÃ§a leitura direta do ChannelTitle
    if not info.get("titulo"):
        try:
            forced = _probe_channel_title_raw(ip, user, password, timeout)
        except Exception:
            forced = None
        if forced:
            info["titulo"] = forced

    modelo_l = str(info.get("modelo") or "").strip().lower()
    fab_l = str(info.get("fabricante") or "").strip().lower()
    hint_txt = f"{modelo_l} {fab_l} {str(txt_model or '').lower()}"
    hint_is_hik = any(k in hint_txt for k in ("hikvision", "hilook", "hi-look", "ds-", "hwi", "hwp"))
    hint_is_unv = any(k in hint_txt for k in ("unv", "uniview", "lapi", "ipc-b"))

    # 3) Enriquecimento via ISAPI (Hikvision/HiLook)
    # Evita timeout desnecessario em Intelbras/Dahua ja identificadas.
    if ((not info.get("modelo")) or (not info.get("fabricante")) or hint_is_hik):
        hik = _probe_hikvision_isapi(ip, user, password, timeout)
        if hik:
            for k in ("modelo", "fabricante", "mac", "serial", "titulo", "firmware"):
                if hik.get(k) and not info.get(k):
                    info[k] = hik[k]

    # 3c) Enriquecimento via LAPI (UNV/Uniview)
    if ((not info.get("modelo")) or (not info.get("fabricante")) or hint_is_unv):
        unv = _probe_unv_lapi(ip, user, password, timeout)
        if unv:
            for k in ("modelo", "fabricante", "mac", "serial", "titulo", "firmware"):
                if unv.get(k) and not info.get(k):
                    info[k] = unv[k]

    

    # 3b) TÃ­tulo do canal em Hikvision/HiLook (ISAPI VideoInput)
    fab = (info.get("fabricante") or "").strip()
    if fab in ("Hikvision", "HiLook"):
        try:
            t = _probe_hikvision_channel_title(ip, user, password, timeout)
        except Exception:
            t = None
        if t:
            info["titulo"] = _normalize_title(t)

    # 4) Enriquecimento via HTML raiz (Intelbras/HiLook)
    # So tenta quando realmente faltam ambos; evita delay em devices ja identificados.
    if (not info.get("modelo")) and (not info.get("fabricante")):
        info = _enrich_from_html_root(ip, user, password, info, timeout)

    # 5) Fallback para modelos antigos (VIP 3220 gen1/gen2 etc.)
    if not info.get("modelo"):
        try:
            guess = best_guess_model(ip, user, password)
            if guess:
                info["modelo"] = guess
                if not info.get("fabricante"):
                    info["fabricante"] = _brand_from_model(guess)
        except Exception:
            pass

    # 6) Fallback final para fabricante
    if not info.get("fabricante"):
        info["fabricante"] = _brand_from_model(info.get("modelo")) or _extract_brand_from_text(txt_model or "")

    # 7) Fallback para MAC (inclui UNV via LAPI /Network/Interfaces)
    if not info.get("mac"):
        try:
            m = get_mac_http(ip, user, password, timeout=timeout, retries=retries)
        except Exception:
            m = None
        if m:
            info["mac"] = m

    return _canonicalize_brand_from_model(info)

def get_mac_http(ip, user, password, timeout=(1.2, 2.5), retries=1):
    urls = [
        f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=Network.eth0",
        f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=Network",
        f"http://{ip}/cgi-bin/magicBox.cgi?action=getSystemInfo",
        f"http://{ip}/ISAPI/System/Network/interfaces/1",
        f"http://{ip}/ISAPI/System/Network",
        f"http://{ip}/LAPI/V1.0/Network/Interfaces",
    ]
    txt = _get_text(urls, user, password, timeout, retries)
    if not txt:
        return None
    m = MAC_RE.search(txt)
    if m:
        return _normalize_mac(m.group(0))
    # UNV normalmente retorna MAC sem separador (ex: c47905b7fe34)
    m12 = MAC_HEX12_RE.search(txt)
    if m12:
        mac12 = _normalize_mac(m12.group(0))
        if mac12:
            return mac12
    for line in txt.splitlines():
        if "PhysicalAddress" in line or "MAC" in line or "Mac" in line:
            m = MAC_RE.search(line)
            if m:
                return _normalize_mac(m.group(0))
            m12 = MAC_HEX12_RE.search(line)
            if m12:
                mac12 = _normalize_mac(m12.group(0))
                if mac12:
                    return mac12
    return None


_NET_FIELD_RE = re.compile(r"\.(IPAddress|SubnetMask|DefaultGateway)\s*=\s*(\S+)", re.IGNORECASE)


def get_network_config(ip, user, password, timeout=(1.5, 3.0), retries=1):
    """Le a config de rede atual da camera (CGI Dahua/Intelbras).

    Retorna {"ip_address", "subnet_mask", "gateway"} ou None se nao
    conseguir ler. Usado pra herdar mascara/gateway ao trocar so o IP.
    """
    urls = [
        f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=Network.eth0",
        f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=Network",
    ]
    txt = _get_text(urls, user, password, timeout, retries)
    if not txt:
        return None
    out = {}
    for m in _NET_FIELD_RE.finditer(txt):
        field, value = m.group(1).lower(), m.group(2).strip()
        key = {"ipaddress": "ip_address", "subnetmask": "subnet_mask", "defaultgateway": "gateway"}[field]
        if key not in out:
            out[key] = value
    return out or None


def set_network_ip(ip, user, password, new_ip, subnet_mask, gateway, timeout=(3.0, 6.0)):
    """Aplica um novo IP (mesma mascara/gateway) direto na camera via CGI
    Dahua/Intelbras (configManager.cgi?action=setConfig). Equipamento vivo:
    se mascara/gateway estiverem errados a camera pode ficar inalcancavel."""
    params = [f"Network.eth0.IPAddress={new_ip}", f"Network.eth0.SubnetMask={subnet_mask}"]
    if gateway:
        params.append(f"Network.eth0.DefaultGateway={gateway}")
    url = f"http://{ip}/cgi-bin/configManager.cgi?action=setConfig&" + "&".join(params)
    r = _try_get(url, user, password, timeout=timeout, use_digest=True, stream=False)
    if r is None or r.status_code != 200:
        r = _try_get(url, user, password, timeout=timeout, use_digest=False, stream=False)
    if r is None:
        return {"ok": False, "error": "sem resposta da camera"}
    text = (r.text or "").strip()
    ok = r.status_code == 200 and "error" not in text.lower()
    return {"ok": ok, "status_code": r.status_code, "response": text[:300]}


def set_channel_title(ip, user, password, title, timeout=(3.0, 6.0)):
    """Grava o titulo/nome (OSD) direto na camera via CGI Dahua/Intelbras
    (configManager.cgi?action=setConfig&ChannelTitle[0].Name=...)."""
    from urllib.parse import quote

    encoded = quote(title, safe="")
    url = f"http://{ip}/cgi-bin/configManager.cgi?action=setConfig&ChannelTitle[0].Name={encoded}"
    r = _try_get(url, user, password, timeout=timeout, use_digest=True, stream=False)
    if r is None or r.status_code != 200:
        r = _try_get(url, user, password, timeout=timeout, use_digest=False, stream=False)
    if r is None:
        return {"ok": False, "error": "sem resposta da camera"}
    text = (r.text or "").strip()
    ok = r.status_code == 200 and "error" not in text.lower()
    return {"ok": ok, "status_code": r.status_code, "response": text[:300]}


def get_snapshot(ip, user, password, output_dir="output/snapshot", timeout=(1.2, 3.0), retries=1):
    os.makedirs(output_dir, exist_ok=True)
    likely_unv = False
    try:
        # Probe de deteccao UNV: cap curto (nao herda o timeout longo do isolado,
        # senao pendura ~15s numa Intelbras/Dahua que nao tem esse endpoint).
        _pt = (min(float(timeout[0]), 1.5), min(float(timeout[1]), 2.0)) if isinstance(timeout, (tuple, list)) else (1.5, 2.0)
        unv_info = _probe_unv_lapi(ip, user, password, _pt)
        likely_unv = bool(unv_info and str(unv_info.get("fabricante") or "").strip().upper() == "UNV")
    except Exception:
        likely_unv = False

    candidates = [
        f"http://{ip}/onvifsnapshot/media?channel=1&subtype=0",
        (_base_http_url(ip) + "/cgi-bin/snapshot.cgi"),
        (_base_http_url(ip) + "/cgi-bin/snapshot.cgi?channel=1"),
        f"http://{ip}/cgi-bin/snapshot.cgi?chn=1",
        f"http://{ip}/ISAPI/Streaming/channels/101/picture",
        f"http://{ip}/snapshot.jpg",
        f"http://{ip}/onvif/snapshot",
    ]
    for url in candidates:
        for _ in range(retries + 1):
            r = _try_get(url, user, password, timeout, stream=True)
            ctype = r.headers.get("Content-Type", "") if r is not None else ""
            if r is not None and r.status_code == 200 and ("image" in ctype or url.endswith("picture")):
                safe_ip = str(ip).replace(":", "__").replace(".", "_").replace("/", "_")
                out = os.path.join(output_dir, f"{safe_ip}.jpg")
                with open(out, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                return out
            r = _try_get(url, user, password, timeout, use_digest=True, stream=True)
            ctype = r.headers.get("Content-Type", "") if r is not None else ""
            if r is not None and r.status_code == 200 and ("image" in ctype or url.endswith("picture")):
                safe_ip = str(ip).replace(":", "__").replace(".", "_").replace("/", "_")
                out = os.path.join(output_dir, f"{safe_ip}.jpg")
                with open(out, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                return out
    
    # Fallback: RTSP (prioriza o path que funcionou # Fallback: RTSP (prioriza o path que funcionou nos testes)
    # Por padrÃ£o, desabilitamos o fallback RTSP para evitar travas em cÃ¢meras offline/zumbi.
    # Se quiser reativar no futuro, defina DISABLE_RTSP_FALLBACK=0 no ambiente.
    import os as _os
    if _os.environ.get("DISABLE_RTSP_FALLBACK", "1") == "1" and not likely_unv:
        return None

    try:
        import cv2  # pip install opencv-python
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

        # timeout pode ser passado como (connect, read) ou nÃºmero simples â€“ aqui usamos a "parte maior"
        rtsp_timeout = 3.0
        if isinstance(timeout, (tuple, list)) and timeout:
            try:
                rtsp_timeout = float(timeout[-1])
            except Exception:
                rtsp_timeout = 3.0
        elif isinstance(timeout, (int, float)):
            rtsp_timeout = float(timeout) or 3.0

        rtsp_candidates = []
        if likely_unv:
            rtsp_candidates.extend([
                f"rtsp://{user}:{password}@{ip}:554/media/video1",
                f"rtsp://{user}:{password}@{ip}:554/media/video2",
                f"rtsp://{user}:{password}@{ip}:554/unicast/c1/s0/live",
                f"rtsp://{user}:{password}@{ip}:554/unicast/c1/s1/live",
            ])
        rtsp_candidates.extend([
            f"rtsp://{user}:{password}@{ip}:554/cam/realmonitor?channel=1&subtype=0",
            f"rtsp://{user}:{password}@{ip}:554/h264/ch1/main/av_stream",
            f"rtsp://{user}:{password}@{ip}:554/user={user}&password={password}&channel=1&stream=0.sdp?",
        ])

        def _rtsp_single(rtsp_url: str):
            cap = cv2.VideoCapture(rtsp_url)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            ok, frame = cap.read()
            cap.release()
            if ok and frame is not None:
                out_path = os.path.join(output_dir, f"{ip}.jpg")
                cv2.imwrite(out_path, frame)
                return out_path
            return None

        for rtsp in rtsp_candidates:
            # Garante que uma cÃ¢mera "zombie" ou offline nÃ£o trave o fluxo:
            with ThreadPoolExecutor(max_workers=1) as executor:
                fut = executor.submit(_rtsp_single, rtsp)
                try:
                    out_path = fut.result(timeout=rtsp_timeout)
                except FuturesTimeoutError:
                    out_path = None
                if out_path:
                    return out_path
    except Exception:
        # Se qualquer erro ocorrer (incluindo indisponibilidade do OpenCV),
        # apenas retorna None e deixa o chamador seguir para o prÃ³ximo IP.
        pass
    return None
