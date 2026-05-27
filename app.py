import json
import os
import random
import re
import time
from datetime import datetime
from difflib import SequenceMatcher
from io import BytesIO
import matplotlib.pyplot as plt
import pandas as pd
import plotly.express as px
import streamlit as st
from gtts import gTTS
from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound
# ============================================================
# Konfiguration
# ============================================================
EXCEL_PATH = "vokabeln.xlsx"
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_AI_LEVEL = "mittel"

BASE_COLUMNS = {
    "ID": "",
    "Deutsch": "",
    "Englisch": "",
    "Kategorie": "Allgemein",
    "Schwierigkeit": DEFAULT_AI_LEVEL,
    "Richtig": 0,
    "Falsch": 0,
    "Zuletzt_geuebt": "",
    "Alternative_Antworten": "",
    "Notizen": "",
}

KI_COLUMNS = {
    "KI_DE_1": "",
    "KI_EN_1": "",
    "KI_DE_2": "",
    "KI_EN_2": "",
    "KI_DE_3": "",
    "KI_EN_3": "",
}

SYNONYM_COLUMNS = {
    "Synonyme_EN": "",
    "Synonyme_DE": "",
    "Antonyme_EN": "",
    "Synonym_Notiz": "",
}


# ------------------------------------------------------------
# 🔐 Passwortschutz
# ------------------------------------------------------------

def check_password():
    """Einfache Passwortabfrage für die Streamlit-App."""

    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False

    if st.session_state.password_correct:
        return True

    st.title("🔐 Vokabeltrainer Login")
    st.info("Bitte Passwort eingeben, um den Vokabeltrainer zu öffnen.")

    # Prüfen, ob das Secret überhaupt vorhanden ist
    if "APP_PASSWORD" not in st.secrets:
        st.error("❌ APP_PASSWORD wurde in den Streamlit Secrets nicht gefunden.")
        st.stop()

    expected_password = str(st.secrets["APP_PASSWORD"]).strip()

    password = st.text_input(
        "Passwort",
        type="password",
        key="password_input"
    )

    if st.button("Einloggen"):
        entered_password = str(password).strip()

        if entered_password == expected_password:
            st.session_state.password_correct = True
            st.rerun()
        else:
            st.error("❌ Passwort ist falsch.")

    return False


if not check_password():
    st.stop()






# ============================================================
# Hilfsfunktionen: Google Sheets / Daten
# ============================================================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]








def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Stellt sicher, dass alle benötigten Spalten existieren."""
    for col, default in {**BASE_COLUMNS, **KI_COLUMNS, **SYNONYM_COLUMNS}.items():
        if col not in df.columns:
            df[col] = default

    # IDs ergänzen, falls leer
    if "ID" in df.columns:
        for idx in df.index:
            if pd.isna(df.at[idx, "ID"]) or str(df.at[idx, "ID"]).strip() == "":
                df.at[idx, "ID"] = f"W{idx + 1:05d}"

    # numerische Spalten robust machen
    for col in ["Richtig", "Falsch"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    # Textspalten robust machen
    text_cols = [c for c in df.columns if c not in ["Richtig", "Falsch"]]
    for col in text_cols:
        df[col] = df[col].fillna("")

    return df


def get_google_sheet_id() -> str | None:
    try:
        if "GOOGLE_SHEET_ID" in st.secrets:
            return str(st.secrets["GOOGLE_SHEET_ID"]).strip()
    except Exception:
        pass
    return os.getenv("GOOGLE_SHEET_ID")


def get_google_sheet_name() -> str:
    try:
        if "GOOGLE_SHEET_NAME" in st.secrets:
            return str(st.secrets["GOOGLE_SHEET_NAME"]).strip() or "Vokabeln"
    except Exception:
        pass
    return os.getenv("GOOGLE_SHEET_NAME", "Vokabeln")


def get_google_credentials_dict() -> dict | None:
    try:
        if "gcp_service_account" in st.secrets:
            creds = dict(st.secrets["gcp_service_account"])
            if "private_key" in creds:
                # Streamlit Secrets speichern Zeilenumbrüche oft als \n.
                creds["private_key"] = str(creds["private_key"]).replace("\\n", "\n")
            return creds
    except Exception:
        pass
    return None


def google_sheets_configured() -> bool:
    return bool(get_google_sheet_id() and get_google_credentials_dict())


def get_google_client():
    creds_dict = get_google_credentials_dict()
    if not creds_dict:
        raise RuntimeError("Google-Service-Account fehlt in Streamlit Secrets unter [gcp_service_account].")
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def get_vocab_worksheet():
    sheet_id = get_google_sheet_id()
    if not sheet_id:
        raise RuntimeError("GOOGLE_SHEET_ID fehlt in Streamlit Secrets.")

    sheet_name = get_google_sheet_name()
    gc = get_google_client()
    try:
        sh = gc.open_by_key(sheet_id)
    except gspread.exceptions.APIError as e:
        st.error("❌ Google Sheets konnte nicht geöffnet werden.")
        st.info("Prüfe: GOOGLE_SHEET_ID, Freigabe an Service Account, echte Google-Tabelle.")
        st.code(str(e), language="text")
        st.stop()

    try:
        return sh.worksheet(sheet_name)
    except WorksheetNotFound:
        # Falls der Tab fehlt, automatisch anlegen.
        ws = sh.add_worksheet(title=sheet_name, rows=1000, cols=50)
        empty_df = pd.DataFrame(columns=list({**BASE_COLUMNS, **KI_COLUMNS, **SYNONYM_COLUMNS}.keys()))
        values = [empty_df.columns.tolist()]
        ws.update(values)
        return ws


def ensure_excel_exists() -> None:
    """Nur noch als lokaler Fallback, falls Google Sheets nicht konfiguriert ist."""
    if not os.path.exists(EXCEL_PATH):
        df = pd.DataFrame(columns=list(BASE_COLUMNS.keys()) + list(KI_COLUMNS.keys()) + list(SYNONYM_COLUMNS.keys()))
        df.to_excel(EXCEL_PATH, index=False)


@st.cache_data(show_spinner=False)
def load_data_cached(source_key: str) -> pd.DataFrame:
    """Lädt Vokabeln aus Google Sheets. Falls nicht konfiguriert: lokaler Excel-Fallback."""
    if google_sheets_configured():
        ws = get_vocab_worksheet()
        values = ws.get_all_values()

        if not values:
            df = pd.DataFrame()
        else:
            headers = values[0]
            rows = values[1:]

            # Leere Spaltenüberschriften automatisch benennen
            clean_headers = []
            seen = {}

            for i, header in enumerate(headers):
                name = str(header).strip()

                if name == "":
                    name = f"Unbenannt_{i+1}"

                # Doppelte Spaltennamen eindeutig machen
                if name in seen:
                    seen[name] += 1
                    name = f"{name}_{seen[name]}"
                else:
                    seen[name] = 1

                clean_headers.append(name)

            df = pd.DataFrame(rows, columns=clean_headers)

            # Komplett leere Zeilen entfernen
            df = df.dropna(how="all")
            df = df[~(df.astype(str).apply(lambda row: "".join(row).strip(), axis=1) == "")]
            
        if df.empty:
            df = pd.DataFrame(columns=list({**BASE_COLUMNS, **KI_COLUMNS, **SYNONYM_COLUMNS}.keys()))
        return ensure_columns(df)

    # Fallback für lokale Tests ohne Google Secrets
    ensure_excel_exists()
    df = pd.read_excel(EXCEL_PATH)
    return ensure_columns(df)


def load_data() -> pd.DataFrame:
    if google_sheets_configured():
        source_key = f"gsheets:{get_google_sheet_id()}:{get_google_sheet_name()}"
    else:
        ensure_excel_exists()
        source_key = f"excel:{os.path.getmtime(EXCEL_PATH)}"
    return load_data_cached(source_key).copy()


def save_data(df: pd.DataFrame) -> None:
    df = ensure_columns(df.copy()).fillna("")

    if google_sheets_configured():
        ws = get_vocab_worksheet()
        values = [df.columns.tolist()] + df.astype(str).values.tolist()
        ws.clear()
        ws.update(values)
    else:
        # Fallback nur für lokale Tests. In Streamlit Cloud bitte Google Sheets verwenden.
        ensure_excel_exists()
        df.to_excel(EXCEL_PATH, index=False)

    st.cache_data.clear()


def find_row_index(df: pd.DataFrame, word_id: str) -> int | None:
    matches = df.index[df["ID"].astype(str) == str(word_id)].tolist()
    return matches[0] if matches else None







# ============================================================
# Hilfsfunktionen: Antwortprüfung
# ============================================================

def normalize_answer(value: str) -> str:
    value = str(value or "").strip().lower()
    value = value.replace("’", "'")
    value = re.sub(r"[.,;:!?()\[\]{}]", "", value)
    value = re.sub(r"\s+", " ", value)
    return value


def answer_variants(main_answer: str, alternatives: str = "") -> set[str]:
    variants = set()
    for item in [main_answer] + re.split(r"[;|,]", str(alternatives or "")):
        norm = normalize_answer(item)
        if norm:
            variants.add(norm)
            # Bei Infinitiven auch Variante ohne "to" erlauben: "to suffer" -> "suffer"
            if norm.startswith("to "):
                variants.add(norm[3:])
    return variants


def is_answer_correct(user_input: str, main_answer: str, alternatives: str = "", tolerance: float = 0.92) -> tuple[bool, str]:
    given = normalize_answer(user_input)
    variants = answer_variants(main_answer, alternatives)
    if given in variants:
        return True, "exakt"

    # leichte Tippfehler tolerieren
    best_score = 0.0
    best_variant = ""
    for variant in variants:
        score = SequenceMatcher(None, given, variant).ratio()
        if score > best_score:
            best_score = score
            best_variant = variant

    if best_score >= tolerance and len(given) >= 4:
        return True, f"toleriert wegen Tippähnlichkeit zu '{best_variant}'"

    return False, "falsch"


def weighted_next_index(df_filtered: pd.DataFrame, already_seen_ids: set[str] | None = None) -> int:
    """Wählt schwierige / selten geübte Wörter bevorzugt aus."""
    if df_filtered.empty:
        return 0

    work = df_filtered.copy().reset_index(drop=True)
    already_seen_ids = already_seen_ids or set()

    richtig = pd.to_numeric(work["Richtig"], errors="coerce").fillna(0)
    falsch = pd.to_numeric(work["Falsch"], errors="coerce").fillna(0)
    total = richtig + falsch
    accuracy_penalty = ((falsch + 1) / (total + 2)) * 4

    # Nie oder lange nicht geübt bevorzugen
    last = pd.to_datetime(work["Zuletzt_geuebt"], errors="coerce")
    days = (pd.Timestamp.today().normalize() - last.dt.normalize()).dt.days
    days = days.fillna(30).clip(lower=0, upper=60)
    recency_bonus = days / 15

    # schon in dieser Session gesehen -> etwas weniger priorisieren
    seen_penalty = work["ID"].astype(str).isin(already_seen_ids).astype(float) * 2

    weights = (1 + accuracy_penalty + recency_bonus - seen_penalty).clip(lower=0.2)
    return int(random.choices(range(len(work)), weights=weights, k=1)[0])

# ============================================================
# Hilfsfunktionen: Audio
# ============================================================

def tts_audio(text: str, lang: str = "en") -> BytesIO:
    tts = gTTS(text=str(text), lang=lang)
    mp3_fp = BytesIO()
    tts.write_to_fp(mp3_fp)
    mp3_fp.seek(0)
    return mp3_fp

# ============================================================
# OpenAI / KI
# ============================================================

def get_openai_api_key() -> str | None:
    try:
        if "OPENAI_API_KEY" in st.secrets:
            return st.secrets["OPENAI_API_KEY"]
    except Exception:
        pass
    return os.getenv("OPENAI_API_KEY")


def get_openai_model() -> str:
    try:
        if "OPENAI_MODEL" in st.secrets:
            return st.secrets["OPENAI_MODEL"]
    except Exception:
        pass
    return DEFAULT_MODEL


def extract_json(text: str):
    text = str(text).strip()
    if text.startswith("[") or text.startswith("{"):
        return json.loads(text)
    start_list = text.find("[")
    end_list = text.rfind("]") + 1
    if start_list >= 0 and end_list > start_list:
        return json.loads(text[start_list:end_list])
    start_obj = text.find("{")
    end_obj = text.rfind("}") + 1
    if start_obj >= 0 and end_obj > start_obj:
        return json.loads(text[start_obj:end_obj])
    raise ValueError("Keine JSON-Struktur gefunden.")


@st.cache_data(show_spinner=False)
def generate_ai_examples(word_de: str, word_en: str, level: str, category: str, variant: int = 0, n: int = 3):
    api_key = get_openai_api_key()
    if not api_key:
        return {"ok": False, "error": "OPENAI_API_KEY fehlt.", "examples": []}

    client = OpenAI(api_key=api_key)
    prompt = f"""
Du bist ein Englischlehrer für einen deutschen Lerner.
Erstelle {n} neue Übungssätze für einen Vokabeltrainer.

Vokabel Deutsch: {word_de}
Vokabel Englisch: {word_en}
Kategorie: {category}
Schwierigkeit: {level}

Ziel:
- Der deutsche Satz ist die Aufgabe.
- Die englische Übersetzung ist die Musterlösung.
- Die englische Übersetzung muss die Vokabel "{word_en}" natürlich und korrekt verwenden.

Regeln:
- Keine Wiederholungen.
- Bei "leicht": kurze Sätze und einfache Grammatik.
- Bei "mittel": natürliche Alltag-/Business-Sätze.
- Bei "komplex": fachlicher oder längerer Satz mit Nebensatz.
- Bei Business-, Energie-, Gas- oder Risk-Management-Themen fachnah formulieren.
- Antworte ausschließlich als JSON-Liste ohne Markdown.

JSON-Format:
[
  {{"deutscher_satz": "...", "englischer_satz": "..."}}
]
"""
    try:
        response = client.responses.create(model=get_openai_model(), input=prompt)
        data = extract_json(response.output_text)
        cleaned = []
        for ex in data:
            de = str(ex.get("deutscher_satz", "")).strip()
            en = str(ex.get("englischer_satz", "")).strip()
            if de and en:
                cleaned.append({"deutscher_satz": de, "englischer_satz": en})
        return {"ok": True, "error": "", "examples": cleaned[:n]}
    except Exception as e:
        return {"ok": False, "error": str(e), "examples": []}


def ai_feedback(word_de: str, word_en: str, user_answer: str, category: str):
    api_key = get_openai_api_key()
    if not api_key:
        return {"ok": False, "error": "OPENAI_API_KEY fehlt.", "feedback": ""}

    client = OpenAI(api_key=api_key)
    prompt = f"""
Du bist ein Englischlehrer für einen deutschen Lerner.
Bewerte die Antwort kurz und hilfreich.

Deutsch: {word_de}
Gesuchte englische Lösung: {word_en}
Antwort des Lerners: {user_answer}
Kategorie: {category}

Gib aus:
- ob die Antwort korrekt, teilweise korrekt oder falsch ist
- die natürliche richtige Formulierung
- eine kurze Erklärung auf Deutsch
- ein englisches Beispiel

Antworte ausschließlich als JSON-Objekt:
{{
  "bewertung": "korrekt|teilweise|falsch",
  "kurze_erklaerung": "...",
  "bessere_antwort": "...",
  "beispiel": "..."
}}
"""
    try:
        response = client.responses.create(model=get_openai_model(), input=prompt)
        data = extract_json(response.output_text)
        parts = [
            f"**Bewertung:** {data.get('bewertung', '')}",
            f"**Erklärung:** {data.get('kurze_erklaerung', '')}",
            f"**Bessere Antwort:** {data.get('bessere_antwort', '')}",
            f"**Beispiel:** {data.get('beispiel', '')}",
        ]
        return {"ok": True, "error": "", "feedback": "\n\n".join(parts)}
    except Exception as e:
        return {"ok": False, "error": str(e), "feedback": ""}




def generate_ai_synonyms(word_de: str, word_en: str, category: str, level: str = "mittel", variant: int = 0):
    """Erzeugt kontextbezogene Synonyme/Antonyme für eine Vokabel."""
    api_key = get_openai_api_key()
    if not api_key:
        return {"ok": False, "error": "OPENAI_API_KEY fehlt.", "data": {}}

    client = OpenAI(api_key=api_key)
    prompt = f"""
Du bist ein Englischlehrer für einen deutschen Lerner.
Erstelle sinnvolle Synonyme für eine Vokabel. Achte streng auf den Kontext.

Deutsch: {word_de}
Englisch: {word_en}
Kategorie: {category}
Schwierigkeit: {level}
Variante: {variant}

Regeln:
- Gib maximal 6 englische Synonyme oder sehr nahe Alternativen aus.
- Gib maximal 6 deutsche Synonyme/Näherungen aus.
- Gib maximal 4 englische Antonyme aus, nur wenn sinnvoll.
- Nimm nur Alternativen auf, die als Antwort in einem Vokabeltrainer vertretbar wären.
- Falls etwas kontextabhängig ist, erkläre es kurz in der Notiz.
- Keine Markdown-Ausgabe, ausschließlich JSON.

JSON-Format:
{{
  "synonyme_en": ["...", "..."],
  "synonyme_de": ["...", "..."],
  "antonyme_en": ["...", "..."],
  "notiz_de": "..."
}}
"""
    try:
        response = client.responses.create(model=get_openai_model(), input=prompt)
        data = extract_json(response.output_text)

        def clean_list(values, limit):
            if not isinstance(values, list):
                return []
            cleaned = []
            seen = set()
            for value in values:
                text = str(value).strip()
                key = text.lower()
                if text and key not in seen:
                    cleaned.append(text)
                    seen.add(key)
            return cleaned[:limit]

        result = {
            "synonyme_en": clean_list(data.get("synonyme_en", []), 6),
            "synonyme_de": clean_list(data.get("synonyme_de", []), 6),
            "antonyme_en": clean_list(data.get("antonyme_en", []), 4),
            "notiz_de": str(data.get("notiz_de", "")).strip(),
        }
        return {"ok": True, "error": "", "data": result}
    except Exception as e:
        return {"ok": False, "error": str(e), "data": {}}



def generate_ai_vocabulary(topic: str, count: int, category: str, level: str = "mittel", include_examples: bool = True, variant: int = 0):
    """Erzeugt neue Vokabelvorschläge zu einem frei vorgegebenen Themenbereich."""
    api_key = get_openai_api_key()
    if not api_key:
        return {"ok": False, "error": "OPENAI_API_KEY fehlt.", "data": []}

    client = OpenAI(api_key=api_key)
    examples_instruction = """
- Erstelle je Vokabel 2 deutsche Beispielsätze und die passenden englischen Musterlösungen.
- Die englischen Beispielsätze müssen die englische Vokabel natürlich verwenden.
""" if include_examples else """
- Lasse die Beispielsatz-Felder leer.
"""

    prompt = f"""
Du bist ein Englischlehrer für einen deutschen Lerner mit Fokus auf Business English, Energy Markets und Gas Storage.
Erstelle {count} neue, nützliche Vokabeln für einen Vokabeltrainer.

Themenbereich: {topic}
Ziel-Kategorie: {category}
Schwierigkeit: {level}
Variante: {variant}

Regeln:
- Wähle praxisnahe Begriffe, keine unnötig exotischen Wörter.
- Deutsch und Englisch müssen fachlich korrekt sein.
- Keine Duplikate innerhalb der Liste.
- Synonyme nur aufnehmen, wenn sie im Kontext als Antwort vertretbar sind.
- Alternative Antworten mit sinnvollen Varianten ergänzen, z. B. ohne "to" bei Verben.
{examples_instruction}
- Antworte ausschließlich als JSON-Liste ohne Markdown.

JSON-Format:
[
  {{
    "Deutsch": "...",
    "Englisch": "...",
    "Kategorie": "...",
    "Schwierigkeit": "leicht|mittel|komplex",
    "Alternative_Antworten": "Antwort 1; Antwort 2",
    "Synonyme_EN": "synonym 1; synonym 2",
    "Synonyme_DE": "Synonym 1; Synonym 2",
    "Antonyme_EN": "antonym 1; antonym 2",
    "Notizen": "kurze Lernnotiz auf Deutsch",
    "KI_DE_1": "...",
    "KI_EN_1": "...",
    "KI_DE_2": "...",
    "KI_EN_2": "...",
    "KI_DE_3": "",
    "KI_EN_3": ""
  }}
]
"""
    try:
        response = client.responses.create(model=get_openai_model(), input=prompt)
        data = extract_json(response.output_text)
        if not isinstance(data, list):
            raise ValueError("Die KI-Antwort war keine JSON-Liste.")

        cleaned = []
        for item in data:
            if not isinstance(item, dict):
                continue
            de = str(item.get("Deutsch", "")).strip()
            en = str(item.get("Englisch", "")).strip()
            if not de or not en:
                continue
            lvl = str(item.get("Schwierigkeit", level)).strip().lower()
            if lvl not in ["leicht", "mittel", "komplex"]:
                lvl = level
            cleaned.append({
                "Deutsch": de,
                "Englisch": en,
                "Kategorie": str(item.get("Kategorie", category)).strip() or category,
                "Schwierigkeit": lvl,
                "Alternative_Antworten": str(item.get("Alternative_Antworten", "")).strip(),
                "Synonyme_EN": str(item.get("Synonyme_EN", "")).strip(),
                "Synonyme_DE": str(item.get("Synonyme_DE", "")).strip(),
                "Antonyme_EN": str(item.get("Antonyme_EN", "")).strip(),
                "Synonym_Notiz": str(item.get("Synonym_Notiz", "")).strip(),
                "Notizen": str(item.get("Notizen", "")).strip(),
                "KI_DE_1": str(item.get("KI_DE_1", "")).strip(),
                "KI_EN_1": str(item.get("KI_EN_1", "")).strip(),
                "KI_DE_2": str(item.get("KI_DE_2", "")).strip(),
                "KI_EN_2": str(item.get("KI_EN_2", "")).strip(),
                "KI_DE_3": str(item.get("KI_DE_3", "")).strip(),
                "KI_EN_3": str(item.get("KI_EN_3", "")).strip(),
            })
        return {"ok": True, "error": "", "data": cleaned[:count]}
    except Exception as e:
        return {"ok": False, "error": str(e), "data": []}


def is_duplicate_word(df: pd.DataFrame, deutsch: str, englisch: str) -> bool:
    """Prüft robuste Dubletten anhand Deutsch oder Englisch."""
    de_norm = normalize_answer(deutsch)
    en_norm = normalize_answer(englisch)
    if df.empty:
        return False
    existing_de = df["Deutsch"].astype(str).map(normalize_answer) if "Deutsch" in df.columns else pd.Series([], dtype=str)
    existing_en = df["Englisch"].astype(str).map(normalize_answer) if "Englisch" in df.columns else pd.Series([], dtype=str)
    return bool((existing_de.eq(de_norm) | existing_en.eq(en_norm)).any())


def build_new_word_id(df: pd.DataFrame, offset: int = 1) -> str:
    """Erzeugt eine robuste neue ID."""
    return f"W{len(df) + offset:05d}_{int(time.time())}"

def create_cloze_sentence(sentence: str, word_en: str) -> str:
    """Einfacher Lückentext: ersetzt die Vokabel oder einzelne Bestandteile."""
    sentence = str(sentence)
    word = str(word_en).strip()
    if not sentence or not word:
        return sentence

    # exakte Phrase ersetzen
    pattern = re.compile(re.escape(word), flags=re.IGNORECASE)
    if pattern.search(sentence):
        return pattern.sub("_____", sentence, count=1)

    # falls mit "to": nur Grundverb ersetzen
    if word.lower().startswith("to "):
        base = word[3:].strip()
        pattern = re.compile(r"\b" + re.escape(base) + r"\b", flags=re.IGNORECASE)
        if pattern.search(sentence):
            return pattern.sub("_____", sentence, count=1)

    return sentence + "  → _____"

# ============================================================
# UI Setup
# ============================================================
st.set_page_config(page_title="Vokabeltrainer", page_icon="📘", layout="wide")
st.title("📘 Intelligenter Vokabeltrainer")
st.caption("Mit KI-Beispielsätzen, Lernpriorisierung, Tests, Dashboard, Admin-Bereich und Google-Sheets-Speicherung")

# Daten laden
df = load_data()

if df.empty:
    st.warning("Deine Vokabelliste enthält noch keine Vokabeln. Öffne den Admin-Bereich und lege die erste Vokabel an.")

# Session State
st.session_state.setdefault("session_seen_ids", set())
st.session_state.setdefault("current_word_id", None)
st.session_state.setdefault("antwort_gegeben", False)
st.session_state.setdefault("antwort_richtig", None)
st.session_state.setdefault("antwort_hinweis", "")
st.session_state.setdefault("reset_antwort", False)
st.session_state.setdefault("last_ai_examples", [])
st.session_state.setdefault("last_ai_synonyms", None)
st.session_state.setdefault("last_generated_vocabulary", [])
st.session_state.setdefault("vocab_generator_refresh", 0)
st.session_state.setdefault("ai_refresh", 0)
st.session_state.setdefault("synonym_refresh", 0)
st.session_state.setdefault("test_aktiv", False)
st.session_state.setdefault("test_vokabeln", None)
st.session_state.setdefault("test_index", 0)
st.session_state.setdefault("test_ergebnisse", [])
st.session_state.setdefault("test_direction", "Deutsch → Englisch")

tab_training, tab_test, tab_dashboard, tab_admin, tab_generator, tab_settings = st.tabs([
    "🏋️ Training",
    "🎓 Test",
    "📊 Dashboard",
    "🛠️ Admin",
    "➕ KI-Vokabelgenerator",
    "⚙️ Einstellungen",
])

# ============================================================
# Training
# ============================================================
with tab_training:
    st.header("🏋️‍♂️ Training")

    if df.empty:
        st.stop()

    categories = sorted([str(x) for x in df["Kategorie"].dropna().unique() if str(x).strip()])
    selected_category = st.selectbox("Kategorie auswählen:", categories if categories else ["Allgemein"])
    mode = st.radio(
        "Lernmodus:",
        ["Deutsch → Englisch", "Englisch → Deutsch", "Lückentext"],
        horizontal=True,
    )

    filtered = df[df["Kategorie"].astype(str) == str(selected_category)].copy().reset_index(drop=True)
    if filtered.empty:
        st.warning("Keine Vokabeln in dieser Kategorie gefunden.")
        st.stop()

    col_prog1, col_prog2, col_prog3 = st.columns(3)
    seen_count = len(set(filtered["ID"].astype(str)).intersection(st.session_state.session_seen_ids))
    with col_prog1:
        st.metric("Vokabeln in Kategorie", len(filtered))
    with col_prog2:
        st.metric("In dieser Session gesehen", seen_count)
    with col_prog3:
        avg_acc = ((filtered["Richtig"].sum()) / max((filtered["Richtig"].sum() + filtered["Falsch"].sum()), 1)) * 100
        st.metric("Trefferquote Kategorie", f"{avg_acc:.0f}%")

    st.progress(seen_count / max(len(filtered), 1))

    with st.expander("📄 Vokabelliste dieser Kategorie anzeigen"):
        for idx, row_list in filtered[["Deutsch", "Englisch", "Richtig", "Falsch"]].iterrows():
            c1, c2, c3, c4 = st.columns([3, 3, 1, 1])
            c1.markdown(f"**🇩🇪 {row_list['Deutsch']}**")
            c2.markdown(f"**🇬🇧 {row_list['Englisch']}**")
            c3.caption(f"✅ {row_list['Richtig']}")
            c4.caption(f"❌ {row_list['Falsch']}")

    def set_new_word():
        next_local_idx = weighted_next_index(filtered, st.session_state.session_seen_ids)
        st.session_state.current_word_id = str(filtered.iloc[next_local_idx]["ID"])
        st.session_state.antwort_gegeben = False
        st.session_state.antwort_richtig = None
        st.session_state.antwort_hinweis = ""
        st.session_state.reset_antwort = True
        st.session_state.last_ai_examples = []
        st.session_state.last_ai_synonyms = None
        st.session_state.ai_refresh += 1
        st.session_state.synonym_refresh += 1

    # initiale Vokabel setzen oder bei Kategorienwechsel reparieren
    valid_ids = set(filtered["ID"].astype(str))
    if st.session_state.current_word_id not in valid_ids:
        set_new_word()

    original_idx = find_row_index(df, st.session_state.current_word_id)
    if original_idx is None:
        set_new_word()
        original_idx = find_row_index(df, st.session_state.current_word_id)

    row = df.loc[original_idx]
    vokabel_de = str(row["Deutsch"]).strip()
    vokabel_en = str(row["Englisch"]).strip()
    alternatives = str(row.get("Alternative_Antworten", "")).strip()
    synonyms_en = str(row.get("Synonyme_EN", "")).strip()
    synonyms_de = str(row.get("Synonyme_DE", "")).strip()
    antonyms_en = str(row.get("Antonyme_EN", "")).strip()
    synonym_note = str(row.get("Synonym_Notiz", "")).strip()
    accepted_answers = "; ".join([x for x in [alternatives, synonyms_en] if str(x).strip()])
    level_default = str(row.get("Schwierigkeit", DEFAULT_AI_LEVEL)).strip().lower() or DEFAULT_AI_LEVEL
    if level_default not in ["leicht", "mittel", "komplex"]:
        level_default = DEFAULT_AI_LEVEL

    st.markdown("---")

    if mode == "Deutsch → Englisch":
        st.subheader(f"Übersetze ins Englische: **{vokabel_de}**")
        expected_answer = vokabel_en
        accepted_for_mode = accepted_answers
        input_label = "Englische Antwort eingeben:"
    elif mode == "Englisch → Deutsch":
        st.subheader(f"Übersetze ins Deutsche: **{vokabel_en}**")
        expected_answer = vokabel_de
        accepted_for_mode = synonyms_de
        input_label = "Deutsche Antwort eingeben:"
    else:
        # Für Lückentext bevorzugt gespeicherte oder frisch erzeugte KI-Sätze verwenden
        base_sentence = ""
        for i in range(1, 4):
            if str(row.get(f"KI_EN_{i}", "")).strip():
                base_sentence = str(row.get(f"KI_EN_{i}", "")).strip()
                break
        if not base_sentence:
            base_sentence = f"I need to use the word {vokabel_en} correctly."
        st.subheader("Fülle die Lücke:")
        st.info(create_cloze_sentence(base_sentence, vokabel_en))
        expected_answer = vokabel_en
        accepted_for_mode = accepted_answers
        input_label = "Englische Antwort eingeben:"

    if st.session_state.reset_antwort:
        st.session_state.antwort = ""
        st.session_state.reset_antwort = False

    def check_training_answer():
        user_input = st.session_state.get("antwort", "")
        correct, reason = is_answer_correct(user_input, expected_answer, accepted_for_mode)
        st.session_state.antwort_gegeben = True
        st.session_state.antwort_richtig = correct
        st.session_state.antwort_hinweis = reason

        idx = find_row_index(df, st.session_state.current_word_id)
        if idx is not None:
            if correct:
                df.at[idx, "Richtig"] = int(df.at[idx, "Richtig"]) + 1
            else:
                df.at[idx, "Falsch"] = int(df.at[idx, "Falsch"]) + 1
            df.at[idx, "Zuletzt_geuebt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_data(df)

    st.text_input(input_label, key="antwort", on_change=check_training_answer)

    c_next1, c_next2, c_next3 = st.columns([1, 1, 2])
    with c_next1:
        if st.button("➡️ Nächste Vokabel", use_container_width=True):
            st.session_state.session_seen_ids.add(str(st.session_state.current_word_id))
            set_new_word()
            st.rerun()
    with c_next2:
        if st.button("🔊 Anhören", use_container_width=True):
            st.audio(tts_audio(vokabel_en, "en"), format="audio/mp3")

    if st.session_state.antwort_gegeben:
        if st.session_state.antwort_richtig:
            st.success(f"✅ Deine Antwort ist korrekt! ({st.session_state.antwort_hinweis})")
        else:
            st.error(f"❌ Leider falsch – richtig wäre: **{expected_answer}**")
            if accepted_for_mode:
                st.caption(f"Auch akzeptiert: {accepted_for_mode}")

            if mode in ["Deutsch → Englisch", "Lückentext"]:
                with st.expander("🤖 KI-Feedback zu meiner Antwort"):
                    if st.button("Feedback erzeugen", key=f"feedback_{st.session_state.current_word_id}"):
                        with st.spinner("KI analysiert deine Antwort ..."):
                            fb = ai_feedback(vokabel_de, vokabel_en, st.session_state.get("antwort", ""), selected_category)
                        if fb["ok"]:
                            st.markdown(fb["feedback"])
                        else:
                            st.warning(f"Feedback konnte nicht erzeugt werden: {fb['error']}")
            else:
                st.info("Für Deutsch-Antworten wird aktuell keine KI-Fehleranalyse erzeugt. Die Synonyme_DE werden aber als erlaubte Antworten berücksichtigt.")

    # Synonyme
    st.markdown("---")
    st.markdown("### 🔎 Synonyme")
    st.caption("Synonyme können angezeigt, per KI erzeugt und in Google Sheets gespeichert werden. Englisch-Synonyme werden zusätzlich als erlaubte Antworten gewertet.")

    syn_col1, syn_col2, syn_col3 = st.columns([1, 1, 1])
    with syn_col1:
        st.markdown("**Englische Synonyme**")
        st.write(synonyms_en if synonyms_en else "–")
    with syn_col2:
        st.markdown("**Deutsche Synonyme**")
        st.write(synonyms_de if synonyms_de else "–")
    with syn_col3:
        st.markdown("**Englische Antonyme**")
        st.write(antonyms_en if antonyms_en else "–")

    if synonym_note:
        st.info(f"Notiz: {synonym_note}")

    syn_btn1, syn_btn2 = st.columns([1, 2])
    with syn_btn1:
        generate_syn_clicked = st.button("🔎 KI-Synonyme erzeugen", use_container_width=True)
    with syn_btn2:
        st.caption("Tipp: Prüfe KI-Synonyme kurz fachlich, bevor du sie dauerhaft speicherst.")

    if generate_syn_clicked:
        with st.spinner("KI erzeugt Synonyme ..."):
            syn_result = generate_ai_synonyms(
                word_de=vokabel_de,
                word_en=vokabel_en,
                category=selected_category,
                level=level_default,
                variant=st.session_state.synonym_refresh,
            )
        st.session_state.synonym_refresh += 1
        if syn_result["ok"]:
            st.session_state.last_ai_synonyms = {
                "word_id": st.session_state.current_word_id,
                **syn_result["data"],
            }
        else:
            st.warning(f"Synonyme konnten nicht erzeugt werden: {syn_result['error']}")

    syn_data = st.session_state.last_ai_synonyms
    if syn_data and syn_data.get("word_id") == st.session_state.current_word_id:
        with st.container(border=True):
            st.markdown("**KI-Vorschlag**")
            st.write("**Synonyme EN:** " + ("; ".join(syn_data.get("synonyme_en", [])) or "–"))
            st.write("**Synonyme DE:** " + ("; ".join(syn_data.get("synonyme_de", [])) or "–"))
            st.write("**Antonyme EN:** " + ("; ".join(syn_data.get("antonyme_en", [])) or "–"))
            if syn_data.get("notiz_de"):
                st.info(syn_data.get("notiz_de"))

            save_syn_col1, save_syn_col2 = st.columns([1, 1])
            with save_syn_col1:
                if st.button("💾 Synonyme speichern", use_container_width=True):
                    idx = find_row_index(df, st.session_state.current_word_id)
                    if idx is not None:
                        df.at[idx, "Synonyme_EN"] = "; ".join(syn_data.get("synonyme_en", []))
                        df.at[idx, "Synonyme_DE"] = "; ".join(syn_data.get("synonyme_de", []))
                        df.at[idx, "Antonyme_EN"] = "; ".join(syn_data.get("antonyme_en", []))
                        df.at[idx, "Synonym_Notiz"] = syn_data.get("notiz_de", "")
                        save_data(df)
                        st.success("Synonyme wurden in der Excel-Datei gespeichert.")
                        st.rerun()
            with save_syn_col2:
                if st.button("➕ Zu alternativen Antworten hinzufügen", use_container_width=True):
                    idx = find_row_index(df, st.session_state.current_word_id)
                    if idx is not None:
                        existing = str(df.at[idx, "Alternative_Antworten"] or "").strip()
                        additions = "; ".join(syn_data.get("synonyme_en", []))
                        df.at[idx, "Alternative_Antworten"] = "; ".join([x for x in [existing, additions] if x])
                        save_data(df)
                        st.success("Synonyme wurden zusätzlich zu Alternative_Antworten hinzugefügt.")
                        st.rerun()

    # KI-Beispielsätze
    st.markdown("---")
    st.markdown("### 🤖 KI-Beispielsätze")
    col_ai_a, col_ai_b, col_ai_c = st.columns([1, 1, 1])
    with col_ai_a:
        ai_level = st.selectbox("Schwierigkeit:", ["leicht", "mittel", "komplex"], index=["leicht", "mittel", "komplex"].index(level_default))
    with col_ai_b:
        use_saved_first = st.checkbox("Gespeicherte Sätze zuerst anzeigen", value=True)
    with col_ai_c:
        auto_generate = st.checkbox("Automatisch generieren", value=False)

    saved_examples = []
    for i in range(1, 4):
        de = str(row.get(f"KI_DE_{i}", "")).strip()
        en = str(row.get(f"KI_EN_{i}", "")).strip()
        if de and en:
            saved_examples.append({"deutscher_satz": de, "englischer_satz": en, "source": "saved"})

    if use_saved_first and saved_examples:
        st.caption("Gespeicherte KI-Sätze aus deiner Excel-Datei:")
        examples_to_show = saved_examples
    else:
        examples_to_show = st.session_state.last_ai_examples

    if st.button("🔄 Neue KI-Beispielsätze erzeugen") or (auto_generate and not examples_to_show):
        with st.spinner("KI erstellt Beispielsätze ..."):
            result = generate_ai_examples(vokabel_de, vokabel_en, ai_level, selected_category, st.session_state.ai_refresh, 3)
        st.session_state.ai_refresh += 1
        if not result["ok"]:
            st.warning(f"KI-Beispielsätze konnten nicht erzeugt werden: {result['error']}")
        else:
            st.session_state.last_ai_examples = result["examples"]
            examples_to_show = result["examples"]

    if not examples_to_show:
        st.info("Klicke auf **Neue KI-Beispielsätze erzeugen**. Dadurch entstehen API-Kosten nur bewusst auf Knopfdruck.")
    else:
        for i, ex in enumerate(examples_to_show, start=1):
            with st.container(border=True):
                st.info(ex["deutscher_satz"])
                b1, b2, b3 = st.columns([1, 1, 2])
                with b1:
                    if st.button(f"💬 Lösung {i}", key=f"show_ai_{i}_{st.session_state.ai_refresh}_{st.session_state.current_word_id}"):
                        st.success(ex["englischer_satz"])
                with b2:
                    if st.button(f"🔊 Satz {i}", key=f"audio_ai_{i}_{st.session_state.ai_refresh}_{st.session_state.current_word_id}"):
                        st.audio(tts_audio(ex["englischer_satz"], "en"), format="audio/mp3")
                with b3:
                    if st.button(f"💾 Satz {i} speichern", key=f"save_ai_{i}_{st.session_state.ai_refresh}_{st.session_state.current_word_id}"):
                        idx = find_row_index(df, st.session_state.current_word_id)
                        if idx is not None:
                            slot = None
                            for j in range(1, 4):
                                if not str(df.at[idx, f"KI_DE_{j}"]).strip() and not str(df.at[idx, f"KI_EN_{j}"]).strip():
                                    slot = j
                                    break
                            if slot is None:
                                slot = 1  # überschreibt ältesten Slot
                            df.at[idx, f"KI_DE_{slot}"] = ex["deutscher_satz"]
                            df.at[idx, f"KI_EN_{slot}"] = ex["englischer_satz"]
                            save_data(df)
                            st.success(f"Gespeichert in KI_DE_{slot}/KI_EN_{slot}.")

    # Statistik aktuelle Vokabel
    with st.expander("📊 Statistik zu dieser Vokabel"):
        r = int(row.get("Richtig", 0) or 0)
        f = int(row.get("Falsch", 0) or 0)
        st.write(f"Richtig: **{r}** | Falsch: **{f}** | Zuletzt geübt: **{row.get('Zuletzt_geuebt', '–')}**")
        if r + f > 0:
            fig, ax = plt.subplots(figsize=(3, 3))
            ax.pie([r, f], labels=["Richtig", "Falsch"], autopct="%1.1f%%", startangle=90)
            ax.axis("equal")
            st.pyplot(fig)

# ============================================================
# Test
# ============================================================
with tab_test:
    st.header("🎓 Testmodus")
    if df.empty:
        st.stop()

    categories = sorted([str(x) for x in df["Kategorie"].dropna().unique() if str(x).strip()])

    if not st.session_state.test_aktiv:
        test_kats = st.multiselect("Kategorien für den Test:", categories, default=categories[:1] if categories else [])
        test_direction = st.radio(
            "Abfragerichtung:",
            ["Deutsch → Englisch", "Englisch → Deutsch"],
            horizontal=True,
            key="test_direction_radio",
        )
        test_length = st.slider("Anzahl Fragen:", 5, 50, 25, step=5)
        only_wrong = st.checkbox("Nur schwierige/falsche Wörter bevorzugen", value=True)

        if st.button("🎯 Neuer Test starten", disabled=len(test_kats) == 0):
            pool = df[df["Kategorie"].astype(str).isin([str(k) for k in test_kats])].dropna(subset=["Deutsch", "Englisch"]).copy()
            if pool.empty:
                st.warning("Keine passenden Vokabeln gefunden.")
            else:
                if only_wrong:
                    pool["score"] = pd.to_numeric(pool["Falsch"], errors="coerce").fillna(0) * 3 - pd.to_numeric(pool["Richtig"], errors="coerce").fillna(0)
                    pool = pool.sort_values("score", ascending=False)
                if len(pool) > test_length:
                    pool = pool.sample(n=test_length, random_state=random.randint(0, 99999)) if not only_wrong else pool.head(test_length)
                st.session_state.test_aktiv = True
                st.session_state.test_direction = test_direction
                st.session_state.test_vokabeln = pool.reset_index(drop=True)
                st.session_state.test_index = 0
                st.session_state.test_ergebnisse = []
                st.rerun()
    else:
        test_df = st.session_state.test_vokabeln
        idx = st.session_state.test_index
        c1, c2 = st.columns([1, 1])
        with c1:
            if st.button("🔄 Test zurücksetzen"):
                st.session_state.test_index = 0
                st.session_state.test_ergebnisse = []
                st.rerun()
        with c2:
            if st.button("🆕 Neuen Test konfigurieren"):
                st.session_state.test_aktiv = False
                st.session_state.test_vokabeln = None
                st.session_state.test_index = 0
                st.session_state.test_ergebnisse = []
                st.rerun()

        if idx < len(test_df):
            row_t = test_df.iloc[idx]
            current_test_direction = st.session_state.get("test_direction", "Deutsch → Englisch")
            if current_test_direction == "Englisch → Deutsch":
                question_text = str(row_t["Englisch"])
                expected_test_answer = str(row_t["Deutsch"])
                test_accepted = str(row_t.get("Synonyme_DE", "") or "")
                input_label_test = "Deutsche Übersetzung:"
            else:
                question_text = str(row_t["Deutsch"])
                expected_test_answer = str(row_t["Englisch"])
                test_accepted = "; ".join([
                    str(row_t.get("Alternative_Antworten", "") or ""),
                    str(row_t.get("Synonyme_EN", "") or ""),
                ])
                input_label_test = "Englische Übersetzung:"

            st.subheader(f"Frage {idx + 1}/{len(test_df)} – Übersetze: **{question_text}**")
            st.caption(f"Richtung: {current_test_direction}")
            user_input = st.text_input(input_label_test, key=f"test_input_{idx}")
            if st.button("Antwort prüfen", key=f"test_check_{idx}"):
                correct, reason = is_answer_correct(user_input, expected_test_answer, test_accepted)
                st.session_state.test_ergebnisse.append({
                    "Deutsch": row_t["Deutsch"],
                    "Englisch": row_t["Englisch"],
                    "Richtung": current_test_direction,
                    "Erwartete_Antwort": expected_test_answer,
                    "Antwort": user_input,
                    "Korrekt": correct,
                    "Hinweis": reason,
                })
                real_idx = find_row_index(df, row_t["ID"])
                if real_idx is not None:
                    if correct:
                        df.at[real_idx, "Richtig"] = int(df.at[real_idx, "Richtig"]) + 1
                    else:
                        df.at[real_idx, "Falsch"] = int(df.at[real_idx, "Falsch"]) + 1
                    df.at[real_idx, "Zuletzt_geuebt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    save_data(df)
                st.session_state.test_index += 1
                st.rerun()
        else:
            results = pd.DataFrame(st.session_state.test_ergebnisse)
            richtig = int(results["Korrekt"].sum()) if not results.empty else 0
            total = len(results)
            st.success(f"🎉 Test abgeschlossen: {richtig}/{total} richtig")
            if total > 0:
                fig, ax = plt.subplots(figsize=(3, 3))
                ax.pie([richtig, total - richtig], labels=["Richtig", "Falsch"], autopct="%1.1f%%", startangle=90)
                ax.axis("equal")
                st.pyplot(fig)
                st.dataframe(results, use_container_width=True)

# ============================================================
# Dashboard
# ============================================================
with tab_dashboard:
    st.header("📊 Dashboard")
    if df.empty:
        st.stop()

    work = df.copy()
    work["Total"] = pd.to_numeric(work["Richtig"], errors="coerce").fillna(0) + pd.to_numeric(work["Falsch"], errors="coerce").fillna(0)
    work["Trefferquote"] = work["Richtig"] / work["Total"].replace(0, pd.NA)
    total_richtig = int(work["Richtig"].sum())
    total_falsch = int(work["Falsch"].sum())
    total_answers = total_richtig + total_falsch

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Vokabeln", len(work))
    m2.metric("Antworten gesamt", total_answers)
    m3.metric("Richtig", total_richtig)
    m4.metric("Trefferquote", f"{(total_richtig / max(total_answers, 1)) * 100:.0f}%")

    st.markdown("### Schwierigste Vokabeln")
    difficult = work.sort_values(["Falsch", "Total"], ascending=False).head(20)
    st.dataframe(difficult[["Deutsch", "Englisch", "Kategorie", "Richtig", "Falsch", "Zuletzt_geuebt"]], use_container_width=True)

    st.markdown("### Trefferquote je Kategorie")

    cat = work.groupby("Kategorie", dropna=False)[["Richtig", "Falsch"]].sum().reset_index()
    cat["Gesamt"] = cat["Richtig"] + cat["Falsch"]
    cat["Trefferquote"] = cat["Richtig"] / cat["Gesamt"].replace(0, pd.NA)

    # Für die Tabelle zeigen wir weiterhin alle Kategorien an.
    cat_display = cat.copy()
    cat_display["Trefferquote_%"] = (cat_display["Trefferquote"].fillna(0) * 100).round(1)
    st.dataframe(
        cat_display[["Kategorie", "Richtig", "Falsch", "Gesamt", "Trefferquote_%"]],
        use_container_width=True,
    )

    # Modernes interaktives Diagramm: nur Kategorien mit mindestens einer Antwort anzeigen.
    # Dadurch entstehen keine überfüllten Achsen mehr und die Werte sind per Tooltip prüfbar.
    cat_chart = cat[cat["Gesamt"] > 0].copy()

    if cat_chart.empty:
        st.info("Noch keine Kategorie mit beantworteten Vokabeln vorhanden. Das Diagramm erscheint, sobald du Antworten gespeichert hast.")
    else:
        cat_chart["Kategorie"] = cat_chart["Kategorie"].astype(str).replace("", "Ohne Kategorie")
        cat_chart["Trefferquote_%"] = (cat_chart["Trefferquote"].fillna(0) * 100).round(1)
        cat_chart = cat_chart.sort_values("Trefferquote_%", ascending=True)

        chart_height = max(420, len(cat_chart) * 34)

        fig = px.bar(
            cat_chart,
            x="Trefferquote_%",
            y="Kategorie",
            orientation="h",
            text=cat_chart["Trefferquote_%"].map(lambda x: f"{x:.0f}%"),
            hover_data={
                "Trefferquote_%": ":.1f",
                "Richtig": True,
                "Falsch": True,
                "Gesamt": True,
                "Kategorie": False,
            },
            labels={
                "Trefferquote_%": "Trefferquote (%)",
                "Kategorie": "",
            },
            title="Trefferquote je Kategorie",
        )

        fig.update_traces(
            textposition="outside",
            marker_color="#35D3DF",
            marker_line_width=0,
            opacity=0.9,
            hovertemplate=(
                "<b>%{y}</b><br>"
                "Trefferquote: %{x:.1f}%<br>"
                "Richtig: %{customdata[1]}<br>"
                "Falsch: %{customdata[2]}<br>"
                "Gesamt: %{customdata[3]}"
                "<extra></extra>"
            ),
        )

        fig.update_layout(
            height=chart_height,
            margin=dict(l=10, r=40, t=70, b=20),
            xaxis=dict(range=[0, 105], ticksuffix="%", showgrid=True, zeroline=False),
            yaxis=dict(title="", automargin=True),
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            font=dict(size=13),
            title=dict(x=0.0, xanchor="left"),
            bargap=0.28,
            showlegend=False,
        )

        st.plotly_chart(fig, use_container_width=True)

# ============================================================
# Admin
# ============================================================
with tab_admin:
    st.header("🛠️ Admin-Bereich")

    with st.expander("➕ Neue Vokabel hinzufügen", expanded=False):
        with st.form("add_word_form"):
            new_de = st.text_input("Deutsch")
            new_en = st.text_input("Englisch")
            new_cat = st.text_input("Kategorie", value="Allgemein")
            new_level = st.selectbox("Schwierigkeit", ["leicht", "mittel", "komplex"], index=1)
            new_alt = st.text_input("Alternative Antworten (mit Semikolon trennen)")
            new_syn_en = st.text_input("Synonyme Englisch (mit Semikolon trennen)")
            new_syn_de = st.text_input("Synonyme Deutsch (mit Semikolon trennen)")
            submitted = st.form_submit_button("Speichern")
            if submitted:
                if not new_de.strip() or not new_en.strip():
                    st.warning("Deutsch und Englisch müssen gefüllt sein.")
                else:
                    new_id = f"W{len(df) + 1:05d}_{int(time.time())}"
                    new_row = {**BASE_COLUMNS, **KI_COLUMNS}
                    new_row.update({
                        "ID": new_id,
                        "Deutsch": new_de.strip(),
                        "Englisch": new_en.strip(),
                        "Kategorie": new_cat.strip() or "Allgemein",
                        "Schwierigkeit": new_level,
                        "Alternative_Antworten": new_alt.strip(),
                        "Synonyme_EN": new_syn_en.strip(),
                        "Synonyme_DE": new_syn_de.strip(),
                    })
                    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                    save_data(df)
                    st.success("Vokabel gespeichert.")
                    st.rerun()

    st.markdown("### Daten bearbeiten")
    st.caption("Änderungen in dieser Tabelle werden nach Klick auf 'Änderungen speichern' in Google Sheets gespeichert.")
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True, key="data_editor")
    if st.button("💾 Änderungen speichern"):
        save_data(edited)
        st.success("Google Sheet aktualisiert.")
        st.rerun()

    with st.expander("⬇️ Vokabelliste als Excel herunterladen"):
        buffer = BytesIO()
        ensure_columns(edited.copy()).to_excel(buffer, index=False)
        buffer.seek(0)
        st.download_button(
            "vokabeln.xlsx herunterladen",
            data=buffer,
            file_name="vokabeln.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


# ============================================================
# KI-Vokabelgenerator
# ============================================================
with tab_generator:
    st.header("➕ KI-Vokabelgenerator")
    st.caption(
        "Erzeuge neue Vokabelpakete zu einem Themenbereich. "
        "Die Vorschläge werden zuerst angezeigt und erst nach deiner Bestätigung in Google Sheets gespeichert."
    )

    with st.form("vocab_generator_form"):
        gen_topic = st.text_area(
            "Themenbereich / Prompt",
            value="Gas storage risk management",
            help="Beispiele: Gas storage hedging, Business meetings, Contract negotiations, Energy trading, General Business English",
        )
        gen_col1, gen_col2, gen_col3 = st.columns([1, 1, 1])
        with gen_col1:
            gen_count = st.slider("Anzahl neuer Vokabeln", min_value=3, max_value=30, value=10, step=1)
        with gen_col2:
            existing_categories = sorted([str(x) for x in df["Kategorie"].dropna().unique() if str(x).strip()]) if not df.empty else []
            default_cat = existing_categories[0] if existing_categories else "Allgemein"
            gen_category = st.text_input("Kategorie", value=default_cat)
        with gen_col3:
            gen_level = st.selectbox("Schwierigkeit", ["leicht", "mittel", "komplex"], index=1)
        gen_include_examples = st.checkbox("Direkt Beispielsätze miterzeugen", value=True)
        submitted_generate_vocab = st.form_submit_button("🤖 Vokabeln per KI erzeugen")

    if submitted_generate_vocab:
        if not str(gen_topic).strip():
            st.warning("Bitte gib zuerst einen Themenbereich ein.")
        else:
            with st.spinner("KI erzeugt neue Vokabelvorschläge ..."):
                result = generate_ai_vocabulary(
                    topic=gen_topic.strip(),
                    count=int(gen_count),
                    category=gen_category.strip() or "Allgemein",
                    level=gen_level,
                    include_examples=gen_include_examples,
                    variant=st.session_state.vocab_generator_refresh,
                )
            st.session_state.vocab_generator_refresh += 1
            if result["ok"]:
                proposals = []
                for item in result["data"]:
                    duplicate = is_duplicate_word(df, item.get("Deutsch", ""), item.get("Englisch", ""))
                    proposals.append({"Auswählen": not duplicate, "Duplikat?": "ja" if duplicate else "nein", **item})
                st.session_state.last_generated_vocabulary = proposals
                if proposals:
                    st.success(f"{len(proposals)} Vokabelvorschläge wurden erzeugt.")
                else:
                    st.warning("Die KI hat keine verwertbaren Vokabeln geliefert.")
            else:
                st.warning(f"Vokabeln konnten nicht erzeugt werden: {result['error']}")

    proposals = st.session_state.last_generated_vocabulary
    if proposals:
        st.markdown("### Vorschläge prüfen")
        st.caption("Entferne den Haken bei Vokabeln, die du nicht speichern möchtest. Bereits vorhandene Duplikate werden standardmäßig abgewählt.")
        proposal_df = pd.DataFrame(proposals)
        edited_proposals = st.data_editor(
            proposal_df,
            use_container_width=True,
            num_rows="dynamic",
            key="generated_vocab_editor",
            column_config={
                "Auswählen": st.column_config.CheckboxColumn("Auswählen"),
                "Duplikat?": st.column_config.TextColumn("Duplikat?", disabled=True),
            },
        )

        save_col1, save_col2, save_col3 = st.columns([1, 1, 2])
        with save_col1:
            if st.button("💾 Ausgewählte Vokabeln speichern", use_container_width=True):
                selected = edited_proposals[edited_proposals["Auswählen"] == True].copy()
                if selected.empty:
                    st.warning("Keine Vokabel ausgewählt.")
                else:
                    rows_to_add = []
                    skipped_duplicates = []
                    for _, item in selected.iterrows():
                        de = str(item.get("Deutsch", "")).strip()
                        en = str(item.get("Englisch", "")).strip()
                        if not de or not en:
                            continue
                        if is_duplicate_word(df, de, en):
                            skipped_duplicates.append(f"{de} / {en}")
                            continue

                        new_row = {**BASE_COLUMNS, **KI_COLUMNS, **SYNONYM_COLUMNS}
                        new_row.update({
                            "ID": build_new_word_id(df, len(rows_to_add) + 1),
                            "Deutsch": de,
                            "Englisch": en,
                            "Kategorie": str(item.get("Kategorie", "Allgemein")).strip() or "Allgemein",
                            "Schwierigkeit": str(item.get("Schwierigkeit", DEFAULT_AI_LEVEL)).strip() or DEFAULT_AI_LEVEL,
                            "Alternative_Antworten": str(item.get("Alternative_Antworten", "")).strip(),
                            "Synonyme_EN": str(item.get("Synonyme_EN", "")).strip(),
                            "Synonyme_DE": str(item.get("Synonyme_DE", "")).strip(),
                            "Antonyme_EN": str(item.get("Antonyme_EN", "")).strip(),
                            "Synonym_Notiz": str(item.get("Synonym_Notiz", "")).strip(),
                            "Notizen": str(item.get("Notizen", "")).strip(),
                            "KI_DE_1": str(item.get("KI_DE_1", "")).strip(),
                            "KI_EN_1": str(item.get("KI_EN_1", "")).strip(),
                            "KI_DE_2": str(item.get("KI_DE_2", "")).strip(),
                            "KI_EN_2": str(item.get("KI_EN_2", "")).strip(),
                            "KI_DE_3": str(item.get("KI_DE_3", "")).strip(),
                            "KI_EN_3": str(item.get("KI_EN_3", "")).strip(),
                        })
                        rows_to_add.append(new_row)

                    if rows_to_add:
                        df_new = pd.concat([df, pd.DataFrame(rows_to_add)], ignore_index=True)
                        save_data(df_new)
                        st.success(f"{len(rows_to_add)} neue Vokabeln wurden in Google Sheets gespeichert.")
                        if skipped_duplicates:
                            st.info("Übersprungene Duplikate: " + "; ".join(skipped_duplicates[:10]))
                        st.session_state.last_generated_vocabulary = []
                        st.rerun()
                    else:
                        st.warning("Es wurden keine neuen Vokabeln gespeichert. Möglicherweise waren alle ausgewählten Einträge Duplikate.")
        with save_col2:
            if st.button("🗑️ Vorschläge verwerfen", use_container_width=True):
                st.session_state.last_generated_vocabulary = []
                st.rerun()
        with save_col3:
            st.info("Gespeichert wird dauerhaft in Google Sheets, sofern deine Google-Secrets korrekt gesetzt sind.")

# ============================================================
# Einstellungen
# ============================================================
with tab_settings:
    st.header("⚙️ Einstellungen")
    st.markdown("### OpenAI / KI")
    key_exists = bool(get_openai_api_key())
    st.write("API-Key gefunden:", "✅ ja" if key_exists else "❌ nein")
    st.write("Aktuelles Modell:", get_openai_model())

    st.info(
        "Für Streamlit Cloud den OpenAI-Key und die Google-Sheets-Zugangsdaten unter App settings → Secrets eintragen. "
        "Der Google-Sheet-Tab sollte z. B. `Vokabeln` heißen."
    )

    st.markdown("### Google Sheets Speicher")
    st.write("Google Sheets konfiguriert:", "✅ ja" if google_sheets_configured() else "❌ nein")
    st.write("Sheet-Name:", get_google_sheet_name())
    st.code("""OPENAI_API_KEY = "sk-..."
OPENAI_MODEL = "gpt-4.1-mini"

GOOGLE_SHEET_ID = "deine_sheet_id"
GOOGLE_SHEET_NAME = "Vokabeln"

[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n"
client_email = "...iam.gserviceaccount.com"
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "...""", language="toml")

    st.markdown("### Kostenkontrolle")
    st.write(
        "Die App generiert KI-Beispielsätze standardmäßig nur auf Knopfdruck. "
        "So vermeidest du unnötige API-Anfragen durch Streamlit-Reruns. Die Vokabeldaten werden dauerhaft in Google Sheets gespeichert."
    )

    st.markdown("### Benötigte requirements.txt")
    st.code("""streamlit
pandas
openpyxl
matplotlib
gtts
openai
gspread
google-auth
""", language="text")
