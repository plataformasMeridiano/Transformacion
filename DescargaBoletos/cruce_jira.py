#!/usr/bin/env python3
"""
cruce_jira.py
Control cruzado bidireccional entre PDFs locales y issues de Jira.

Uso:
    python3 cruce_jira.py

Salida:
    /mnt/c/dev/Meridiano/Transformacion/DescargaBoletos/reporte_cruce_jira.csv
"""

import csv
import json
import os
import re
import sys
from pathlib import Path
from collections import defaultdict

import pdfplumber

# ─────────────────────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────────────────────

DOWNLOADS_DIR = Path("/mnt/c/dev/Meridiano/Transformacion/DescargaBoletos/downloads")
JIRA_CSV      = Path("/mnt/c/Users/aduce/Downloads/Jira (9).csv")
OUTPUT_CSV    = Path("/mnt/c/dev/Meridiano/Transformacion/DescargaBoletos/reporte_cruce_jira.csv")

# Mapeo Jira label → carpeta local  (None = no hay PDFs locales para esta ALYC)
ALYC_MAP = {
    "ConoSur":     "ConoSur",
    "ADCAP":       "ADCAP",
    "Max Capital": "MaxCapital",
    "Puente":      "Puente",
    "BACS":        "BACS",
    "Win":         "WIN",
    "Dhalmore":    "Dhalmore",
    "Metrocorp":   "MetroCorp",
    "Criteria":    "Criteria",
    "Cocos":       None,   # sin PDFs locales; se incluirá como Jira-only
}

CANONICAL_FOLDERS = {v for v in ALYC_MAP.values() if v}
FOLDER_TO_LABEL   = {v: k for k, v in ALYC_MAP.items() if v}
ACCOUNT_DIRS      = {"MeridianoNorte", "Pamat"}


def is_artifact(stem: str) -> bool:
    """Stems puramente numéricos con < 4 dígitos son artefactos."""
    return bool(re.fullmatch(r"\d{1,3}", stem))


# ─────────────────────────────────────────────────────────────────────────────
# Normalización de IDs de boleto
# ─────────────────────────────────────────────────────────────────────────────

def norm_id(raw: str) -> str | None:
    """
    Convierte un ID a string entero sin ceros leading.
    Maneja notación científica (2.026035279E9 → '2026035279').
    """
    s = (raw or "").strip()
    if not s:
        return None
    if re.search(r"[Ee][+\-]?\d", s):
        try:
            return str(int(float(s)))
        except ValueError:
            return s
    try:
        return str(int(float(s)))
    except ValueError:
        pass
    if re.fullmatch(r"\d+", s):
        return str(int(s))
    return s


def stem_to_id(stem: str, alyc_folder: str) -> str:
    """Extrae el número de boleto del stem del filename según la ALYC."""
    s = stem
    s = re.sub(r"^BOL_",        "", s, flags=re.IGNORECASE)
    s = re.sub(r"^CD_",         "", s, flags=re.IGNORECASE)
    s = re.sub(r"^BOLETO_NRO_", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^BOLETO_",     "", s, flags=re.IGNORECASE)
    # Dhalmore: "Boleto - Dhalmore - 13846"
    m = re.search(r"-\s*(\d+)\s*$", s)
    if m:
        s = m.group(1)
    if re.fullmatch(r"\d+", s):
        return str(int(s))
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Normalización de fechas
# ─────────────────────────────────────────────────────────────────────────────

_MONTH_MAP = {
    "Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06",
    "Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12",
}

def norm_date_jira(raw: str) -> str | None:
    """'15/Jan/26 00:00' → '2026-01-15'"""
    m = re.match(r"(\d{1,2})/([A-Za-z]{3})/(\d{2})", (raw or "").strip())
    if not m:
        return None
    day = m.group(1).zfill(2)
    mon = m.group(2).capitalize()
    yr  = m.group(3)
    return f"20{yr}-{_MONTH_MAP.get(mon, '??')}-{day}"


# ─────────────────────────────────────────────────────────────────────────────
# Tipo de operación
# ─────────────────────────────────────────────────────────────────────────────

def tipo_from_summary_or_ocr(summary: str, log_data: dict) -> str | None:
    """Infiere 'Cauciones' o 'Pases' desde el OCR JSON o el Summary."""
    tipo_ocr = (log_data.get("tipo_operacion") or "").upper()
    if "CAUCION" in tipo_ocr or "CAUCI" in tipo_ocr:
        return "Cauciones"
    if "PASE" in tipo_ocr:
        return "Pases"
    s = summary.lower()
    if "cauc" in s:
        return "Cauciones"
    if "pase" in s:
        return "Pases"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# PASO 1 — Recorrer PDFs locales
# ─────────────────────────────────────────────────────────────────────────────

_win_cache: dict[str, str] = {}

def _extract_win_boleto(pdf_path: Path) -> str:
    """
    Lee el PDF de WIN y extrae el número de boleto real desde la tabla de cabecera.
    Busca: "Comitente  Operación  Liquidación  Número" → siguiente línea → último número.
    """
    stem = pdf_path.stem
    if stem in _win_cache:
        return _win_cache[stem]
    result = "nro_not_extractable"
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            text = "\n".join(pg.extract_text() or "" for pg in pdf.pages)
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if re.search(r"Comitente\s+Operaci[oó]n\s+Liquidaci[oó]n\s+N[uú]mero", line):
                if i + 1 < len(lines):
                    nums = re.findall(r"\d+", lines[i + 1])
                    if nums:
                        result = str(int(nums[-1]))
                break
    except Exception:
        result = "nro_not_extractable"
    _win_cache[stem] = result
    return result


def _ingest_tipo_dir(tipo_dir: Path, alyc_label: str, folder: str,
                     fecha: str, tipo: str, records: list, seen: set):
    """Lee todos los PDFs no-artefacto de un dir Cauciones/Pases."""
    for pdf in sorted(tipo_dir.iterdir()):
        if pdf.suffix.lower() != ".pdf":
            continue
        stem = pdf.stem
        if is_artifact(stem):
            continue

        nro = _extract_win_boleto(pdf) if folder == "WIN" else stem_to_id(stem, folder)

        # Deduplicar: MeridianoNorte y Pamat tienen el mismo boleto
        dedup_key = (fecha, tipo, nro)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        records.append({
            "alyc":         alyc_label,
            "alyc_folder":  folder,
            "fecha":        fecha,
            "tipo":         tipo,
            "nro":          nro,
            "pdf_path":     str(pdf),
        })


def collect_local_pdfs() -> list[dict]:
    records = []
    for alyc_dir in sorted(DOWNLOADS_DIR.iterdir()):
        if not alyc_dir.is_dir() or alyc_dir.name not in CANONICAL_FOLDERS:
            continue
        folder      = alyc_dir.name
        alyc_label  = FOLDER_TO_LABEL[folder]
        seen: set   = set()   # dedup per ALYC (shared across all dates for safety)

        for date_dir in sorted(alyc_dir.iterdir()):
            if not date_dir.is_dir():
                continue
            fecha = date_dir.name
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha):
                continue

            for item in sorted(date_dir.iterdir()):
                if not item.is_dir():
                    continue
                if item.name in ("Cauciones", "Pases"):
                    _ingest_tipo_dir(item, alyc_label, folder, fecha, item.name, records, seen)
                elif item.name in ACCOUNT_DIRS:
                    for tipo_dir in sorted(item.iterdir()):
                        if tipo_dir.is_dir() and tipo_dir.name in ("Cauciones", "Pases"):
                            _ingest_tipo_dir(tipo_dir, alyc_label, folder, fecha,
                                             tipo_dir.name, records, seen)
    return records


# ─────────────────────────────────────────────────────────────────────────────
# PASO 2 — Cargar issues Jira
# ─────────────────────────────────────────────────────────────────────────────

def load_jira_issues() -> list[dict]:
    issues = []
    with open(JIRA_CSV, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            alyc = row.get("Custom field (ALyC) (Label)", "").strip()
            if alyc not in ALYC_MAP:
                continue
            tipo_boleto = row.get("Custom field (Tipo de Boleto)", "").strip()
            if not tipo_boleto:
                continue

            fecha = norm_date_jira(row.get("Custom field (Fecha de Operación)", ""))
            nro   = norm_id(row.get("Custom field (Identificador en la ALyC)", ""))

            log_raw  = row.get("Custom field (Log Automatización)", "").strip()
            log_data = {}
            if log_raw:
                try:
                    parsed = json.loads(log_raw)
                    if isinstance(parsed, dict) and not parsed.get("isError"):
                        log_data = parsed
                except Exception:
                    pass

            monto_neto = None
            raw_mn = row.get("Custom field (Monto Neto)", "").strip()
            if raw_mn:
                try:
                    monto_neto = float(raw_mn)
                except ValueError:
                    pass

            tasa = None
            raw_t = row.get("Custom field (Tasa)", "").strip()
            if raw_t:
                try:
                    tasa = float(raw_t)
                except ValueError:
                    pass

            summary = row.get("Summary", "")
            tipo_op = tipo_from_summary_or_ocr(summary, log_data)

            issues.append({
                "jira_key":   row.get("Issue key", ""),
                "summary":    summary,
                "alyc":       alyc,
                "fecha":      fecha,
                "tipo":       tipo_op,
                "nro":        nro,
                "monto_neto": monto_neto,
                "tasa":       tasa,
                "log_data":   log_data,
            })
    return issues


# ─────────────────────────────────────────────────────────────────────────────
# PASO 3 — Extraer campos del PDF para validación
# ─────────────────────────────────────────────────────────────────────────────

def _dmy_to_iso(s: str) -> str | None:
    """'15/01/2026' o '15/1/2026' → '2026-01-15'"""
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s.strip())
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    return None


def _parse_ars(s: str) -> float | None:
    """
    Convierte monto argentino a float.
    '1.476.579.166'   → 1476579166.0  (puntos = miles, sin decimal)
    '217.867.476,56'  → 217867476.56  (punto = miles, coma = decimal)
    """
    s = s.strip()
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(".", "")
    s = re.sub(r"[^\d.]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def _parse_comitente_header(text: str) -> dict:
    """
    Parsea tabla de Dhalmore/WIN:
      Comitente  Operación  Liquidación  Número
      100066     02/03/26   02/03/26     13846
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if re.search(r"Comitente\s+Operaci[oó]n\s+Liquidaci[oó]n\s+N[uú]mero", line):
            if i + 1 < len(lines):
                nxt = lines[i + 1]
                result = {}
                dates = re.findall(r"\d{2}/\d{2}/\d{2}", nxt)
                nums  = re.findall(r"\b\d{4,}\b", nxt)
                if dates:
                    d, m, y = dates[0].split("/")
                    result["fecha_operacion"] = f"20{y}-{m}-{d}"
                if nums:
                    result["numero"] = nums[-1]
                return result
    return {}


def extract_pdf_fields(pdf_path: str, alyc_folder: str) -> dict:
    """
    Abre el PDF y extrae: numero_boleto, fecha_boleto, monto_neto, tasa_tna.
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join(pg.extract_text() or "" for pg in pdf.pages)
    except Exception as e:
        return {"error": str(e)}

    out = {
        "numero_boleto": None,
        "fecha_boleto":  None,
        "monto_neto":    None,
        "tasa_tna":      None,
    }

    # ── número de boleto ────────────────────────────────────────────────────
    if alyc_folder == "Puente":
        m = re.search(r"CAUCION\s+(\d+)\s+\d{2}/\d{2}/\d{4}", text)
        if m:
            out["numero_boleto"] = str(int(m.group(1)))

    elif alyc_folder == "ConoSur":
        m = re.search(r"[Bb]oleto\s*[Nn][°º]?\s*(\d{6,})", text)
        if m:
            out["numero_boleto"] = str(int(m.group(1)))

    elif alyc_folder in ("ADCAP", "BACS", "Criteria"):
        m = re.search(r"#\s*([\d,\.]+)", text)
        if m:
            digits = re.sub(r"[,\.]", "", m.group(1))
            out["numero_boleto"] = str(int(digits)) if digits.isdigit() else digits

    elif alyc_folder == "MaxCapital":
        m = re.search(r"#\s*([\d\.]+)", text)
        if m:
            out["numero_boleto"] = m.group(1).replace(".", "")

    elif alyc_folder in ("Dhalmore", "WIN"):
        h = _parse_comitente_header(text)
        out["numero_boleto"] = h.get("numero")

    elif alyc_folder == "MetroCorp":
        m = re.search(r"boleto\s+[Nn][°º]\s*(\d+)", text, re.IGNORECASE)
        if m:
            out["numero_boleto"] = str(int(m.group(1)))

    # ── fecha ────────────────────────────────────────────────────────────────
    if alyc_folder == "Puente":
        m = re.search(r"CAUCION\s+Tomadora\s+(\d{2}/\d{2}/\d{4})", text)
        if not m:
            m = re.search(r"Concertaci[oó]n[:\s]+(\d{1,2}/\d{1,2}/\d{4})", text, re.IGNORECASE)
        if m:
            out["fecha_boleto"] = _dmy_to_iso(m.group(1))

    elif alyc_folder in ("ADCAP", "BACS", "Criteria", "MaxCapital"):
        m = re.search(r"Concertaci[oó]n[:\s]+(\d{1,2}/\d{1,2}/\d{4})", text, re.IGNORECASE)
        if m:
            out["fecha_boleto"] = _dmy_to_iso(m.group(1))

    elif alyc_folder in ("Dhalmore", "WIN"):
        h = _parse_comitente_header(text)
        out["fecha_boleto"] = h.get("fecha_operacion")

    elif alyc_folder == "ConoSur":
        # Primera fecha en el documento
        m = re.search(r"(\d{2}/\d{2}/\d{4})", text)
        if m:
            out["fecha_boleto"] = _dmy_to_iso(m.group(1))

    elif alyc_folder == "MetroCorp":
        m = re.search(r"(\d{2}/\d{2}/\d{4})", text)
        if m:
            out["fecha_boleto"] = _dmy_to_iso(m.group(1))

    # ── monto neto ────────────────────────────────────────────────────────────
    if alyc_folder == "Puente":
        # "Neto a $ 1.476.579.166"  (puntos = miles, sin coma decimal)
        m = re.search(r"Neto\s+a\s+\$\s*([\d\.]+)", text)
        if m:
            out["monto_neto"] = _parse_ars(m.group(1))

    elif alyc_folder in ("ADCAP", "BACS", "Criteria", "MaxCapital"):
        m = re.search(r"[Nn]eto\s+a\s+[Cc]obrar[:\s]*([\d\.]+,\d{2})", text)
        if m:
            out["monto_neto"] = _parse_ars(m.group(1))

    elif alyc_folder in ("Dhalmore", "WIN"):
        m = re.search(r"IMPORTE\s+NETO\s+([\d\.,]+)", text, re.IGNORECASE)
        if m:
            out["monto_neto"] = _parse_ars(m.group(1))
        else:
            m = re.search(r"Capital\s+([\d]+)", text)
            if m:
                out["monto_neto"] = float(m.group(1))

    elif alyc_folder == "ConoSur":
        m = re.search(r"Result\.?:.*?([\d\.]+,\d{2})\s*@", text)
        if not m:
            m = re.search(r"[Tt]otal[^$\d]*([\d\.]+,\d{2})", text)
        if m:
            out["monto_neto"] = _parse_ars(m.group(1))

    elif alyc_folder == "MetroCorp":
        m = re.search(r"TOTAL[^\d]*([\d\.]+,\d{2})", text, re.IGNORECASE)
        if m:
            out["monto_neto"] = _parse_ars(m.group(1))

    # ── tasa TNA ─────────────────────────────────────────────────────────────
    m = re.search(r"T\.?\s*N\.?\s*A\.?\s*[:=]?\s*([\d,\.]+)\s*%", text, re.IGNORECASE)
    if m:
        out["tasa_tna"] = float(m.group(1).replace(",", "."))

    return out


# ─────────────────────────────────────────────────────────────────────────────
# PASO 4 — Validar contenido
# ─────────────────────────────────────────────────────────────────────────────

def validate_content(pdf_fields: dict, issue: dict) -> tuple[bool, list[str]]:
    """Compara campos PDF contra OCR/Jira. Retorna (ok, lista_de_diferencias)."""
    if "error" in pdf_fields:
        return False, [f"pdf_read_error: {pdf_fields['error']}"]

    log   = issue.get("log_data", {})
    diffs = []

    # número de boleto
    pdf_nro  = pdf_fields.get("numero_boleto")
    jira_nro = issue.get("nro")
    if pdf_nro and jira_nro and norm_id(pdf_nro) != norm_id(jira_nro):
        diffs.append(f"nro: pdf={pdf_nro} jira={jira_nro}")

    # fecha
    pdf_fecha = pdf_fields.get("fecha_boleto")
    ref_fecha = issue.get("fecha")
    # Intentar obtener fecha del OCR en ISO
    log_fecha_raw = log.get("fecha_boleto") or log.get("fecha_operacion") or ""
    if log_fecha_raw:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", log_fecha_raw)
        if m:
            ref_fecha = m.group(1)
        else:
            m2 = re.search(r"(\d{2})/(\d{2})/(\d{4})", log_fecha_raw)
            if m2:
                ref_fecha = f"{m2.group(3)}-{m2.group(2)}-{m2.group(1)}"
    if pdf_fecha and ref_fecha and pdf_fecha != ref_fecha:
        diffs.append(f"fecha: pdf={pdf_fecha} jira={ref_fecha}")

    # monto neto — comparar en valor absoluto (Jira puede ser negativo para cobros)
    pdf_monto = pdf_fields.get("monto_neto")
    log_monto = log.get("monto_neto") or log.get("capital")
    ref_monto = float(log_monto) if log_monto is not None else issue.get("monto_neto")
    if pdf_monto is not None and ref_monto is not None:
        a, b = abs(pdf_monto), abs(float(ref_monto))
        diff = abs(a - b)
        tol  = max(1.0, max(a, b) * 0.02)   # 2 % relativo o $1 absoluto
        if diff > tol:
            diffs.append(f"monto_neto: pdf={pdf_monto:,.0f} jira={b:,.0f}")

    # tasa TNA
    pdf_tasa = pdf_fields.get("tasa_tna")
    log_tasa = log.get("tasa_tna")
    ref_tasa = float(log_tasa) if log_tasa is not None else issue.get("tasa")
    if pdf_tasa is not None and ref_tasa is not None:
        if abs(pdf_tasa - float(ref_tasa)) > 0.05:
            diffs.append(f"tasa_tna: pdf={pdf_tasa} jira={ref_tasa}")

    return len(diffs) == 0, diffs


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 68)
    print("CRUCE JIRA ↔ PDFs LOCALES")
    print("=" * 68)

    # 1. PDFs
    print("\n[1/5] Cargando PDFs locales (WIN requiere abrir cada PDF)...",
          flush=True)
    local_pdfs = collect_local_pdfs()
    print(f"      → {len(local_pdfs)} PDFs cargados")

    # 2. Jira
    print("[2/5] Cargando issues Jira...", end=" ", flush=True)
    jira_issues = load_jira_issues()
    print(f"{len(jira_issues)} issues con ALYC y Tipo de Boleto")

    # 3. Índices
    # Clave de match: (alyc_label, fecha, tipo, nro)
    pdf_by_key: dict[tuple, dict] = {}
    for p in local_pdfs:
        pdf_by_key[(p["alyc"], p["fecha"], p["tipo"], p["nro"])] = p

    # 4. Cruce
    print("[3/5] Cruzando issues Jira con PDFs...", flush=True)
    report_rows: list[dict] = []
    matched_pdf_keys: set   = set()
    n = len(jira_issues)

    for idx, iss in enumerate(jira_issues):
        if idx % max(1, n // 20) == 0:
            print(f"      {idx}/{n} issues procesados...", end="\r", flush=True)

        key = (iss["alyc"], iss["fecha"], iss["tipo"], iss["nro"])
        pdf = pdf_by_key.get(key)

        # Fallback: si tipo no se infirió, probar ambos
        if pdf is None and iss["tipo"] is None:
            for t in ("Cauciones", "Pases"):
                k2 = (iss["alyc"], iss["fecha"], t, iss["nro"])
                if k2 in pdf_by_key:
                    pdf = pdf_by_key[k2]
                    key = k2
                    break

        if pdf:
            matched_pdf_keys.add(key)

        content_ok   = "NA"
        content_diff = ""

        if pdf:
            fields = extract_pdf_fields(pdf["pdf_path"], pdf["alyc_folder"])
            ok, diffs = validate_content(fields, iss)
            content_ok   = "True" if ok else "False"
            content_diff = "; ".join(diffs)

        tipo_out = iss["tipo"] or (pdf["tipo"] if pdf else "")
        report_rows.append({
            "source":       "both" if pdf else "Jira",
            "alyc":         iss["alyc"],
            "fecha":        iss["fecha"] or "",
            "tipo":         tipo_out or "",
            "nro_boleto":   iss["nro"] or "",
            "pdf_path":     pdf["pdf_path"] if pdf else "",
            "jira_key":     iss["jira_key"],
            "content_ok":   content_ok,
            "content_diff": content_diff,
        })

    print(f"      {n}/{n} issues procesados.        ")

    # 5. PDFs sin issue Jira
    print("[4/5] PDFs sin issue Jira...", end=" ", flush=True)
    n_pdf_only = 0
    for p in local_pdfs:
        pk = (p["alyc"], p["fecha"], p["tipo"], p["nro"])
        if pk not in matched_pdf_keys:
            n_pdf_only += 1
            report_rows.append({
                "source":       "PDF",
                "alyc":         p["alyc"],
                "fecha":        p["fecha"],
                "tipo":         p["tipo"],
                "nro_boleto":   p["nro"],
                "pdf_path":     p["pdf_path"],
                "jira_key":     "",
                "content_ok":   "NA",
                "content_diff": "",
            })
    print(f"{n_pdf_only} encontrados")

    # 5. CSV
    print("[5/5] Escribiendo CSV...", end=" ", flush=True)
    fieldnames = [
        "source", "alyc", "fecha", "tipo", "nro_boleto",
        "pdf_path", "jira_key", "content_ok", "content_diff",
    ]

    def sort_key(r):
        # Orden: discrepancias primero, luego Jira-only, PDF-only, OK
        pri = {"False": 0, "Jira": 1, "PDF": 2, "True": 3, "NA": 4}
        s = r["source"]
        c = r["content_ok"]
        if s == "both":
            p = 0 if c == "False" else 3
        elif s == "Jira":
            p = 1
        else:
            p = 2
        return (p, r["alyc"], r["fecha"], r["nro_boleto"])

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(sorted(report_rows, key=sort_key))
    print(f"listo ({len(report_rows)} filas)")

    # ── Resumen ───────────────────────────────────────────────────────────────
    n_both      = sum(1 for r in report_rows if r["source"] == "both")
    n_jira_only = sum(1 for r in report_rows if r["source"] == "Jira")
    n_pdf_only2 = sum(1 for r in report_rows if r["source"] == "PDF")
    n_ok        = sum(1 for r in report_rows if r["content_ok"] == "True")
    n_fail      = sum(1 for r in report_rows if r["content_ok"] == "False")

    print()
    print("=" * 68)
    print("RESUMEN GLOBAL")
    print("=" * 68)
    print(f"  Issues Jira (boletos):           {len(jira_issues):>5}")
    print(f"  PDFs locales (dedup):             {len(local_pdfs):>5}")
    print(f"  ─────────────────────────────────────")
    print(f"  Matches bidireccionales:          {n_both:>5}")
    print(f"  Issues Jira SIN PDF local:        {n_jira_only:>5}")
    print(f"  PDFs locales SIN issue Jira:      {n_pdf_only2:>5}")
    print(f"  ─────────────────────────────────────")
    print(f"  Contenido OK  (de los {n_both} matches):  {n_ok:>5}")
    print(f"  Contenido con diferencias:        {n_fail:>5}")

    print()
    hdr = f"  {'ALYC':<13} {'Jira':>5} {'PDFs':>5} {'Match':>6}  {'SinPDF':>6}  {'SinJira':>7}  {'DiffCont':>8}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    all_alycs = sorted(set(r["alyc"] for r in report_rows))
    for alyc in all_alycs:
        rows_a = [r for r in report_rows if r["alyc"] == alyc]
        nj  = sum(1 for r in rows_a if r["jira_key"])
        np  = sum(1 for r in rows_a if r["pdf_path"])
        nm  = sum(1 for r in rows_a if r["source"] == "both")
        nsp = sum(1 for r in rows_a if r["source"] == "Jira")
        nsj = sum(1 for r in rows_a if r["source"] == "PDF")
        nd  = sum(1 for r in rows_a if r["content_ok"] == "False")
        print(f"  {alyc:<13} {nj:>5} {np:>5} {nm:>6}  {nsp:>6}  {nsj:>7}  {nd:>8}")

    print()
    print(f"  CSV → {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
