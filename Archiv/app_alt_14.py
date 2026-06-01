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
SYNC_BATCH_SIZE = 10  # Anzahl Antworten, bevor automatisch nach Google Sheets synchronisiert wird

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

VERB_COLUMNS = {
    "Wortart": "",
    "Verbformen_JSON": "",
    "Verbformen_Notiz": "",
}

HISTORY_COLUMNS = [
    "Zeitstempel",
    "Datum",
    "ID",
    "Deutsch",
    "Englisch",
    "Kategorie",
    "Modus",
    "Antwort",
    "Erwartete_Antwort",
    "Korrekt",
    "Hinweis",
]

SETTINGS_COLUMNS = ["Key", "Value"]

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
    df = df.copy()

    for col, default in {**BASE_COLUMNS, **KI_COLUMNS, **SYNONYM_COLUMNS, **VERB_COLUMNS}.items():
        if col not in df.columns:
            df[col] = default

    # IDs ergänzen, falls leer oder ungültig
    if "ID" in df.columns:
        numeric_ids = pd.to_numeric(df["ID"], errors="coerce")
        max_id = numeric_ids.max()

        if pd.isna(max_id):
            next_id = 1
        else:
            next_id = int(max_id) + 1

        for idx in df.index:
            current_id = pd.to_numeric(df.at[idx, "ID"], errors="coerce")

            if pd.isna(current_id):
                df.at[idx, "ID"] = str(next_id)
                next_id += 1
            else:
                df.at[idx, "ID"] = str(int(current_id))

    # numerische Spalten robust machen
    for col in ["Richtig", "Falsch"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    # Textspalten robust machen
    text_cols = [c for c in df.columns if c not in ["Richtig", "Falsch"]]
    for col in text_cols:
        df[col] = df[col].fillna("").astype(str)

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


@st.cache_resource(show_spinner=False)
def get_google_client_cached(creds_json: str):
    creds_dict = json.loads(creds_json)
    if "private_key" in creds_dict:
        creds_dict["private_key"] = str(creds_dict["private_key"]).replace("\\n", "\n")
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def get_google_client():
    creds_dict = get_google_credentials_dict()
    if not creds_dict:
        raise RuntimeError("Google-Service-Account fehlt in Streamlit Secrets unter [gcp_service_account].")
    creds_json = json.dumps(creds_dict, sort_keys=True)
    return get_google_client_cached(creds_json)


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
        empty_df = pd.DataFrame(columns=list({**BASE_COLUMNS, **KI_COLUMNS, **SYNONYM_COLUMNS, **VERB_COLUMNS}.keys()))
        values = [empty_df.columns.tolist()]
        ws.update(values)
        return ws


def ensure_excel_exists() -> None:
    """Nur noch als lokaler Fallback, falls Google Sheets nicht konfiguriert ist."""
    if not os.path.exists(EXCEL_PATH):
        df = pd.DataFrame(columns=list(BASE_COLUMNS.keys()) + list(KI_COLUMNS.keys()) + list(SYNONYM_COLUMNS.keys()) + list(VERB_COLUMNS.keys()))
        df.to_excel(EXCEL_PATH, index=False)


@st.cache_data(show_spinner=False, ttl=60)
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
            df = pd.DataFrame(columns=list({**BASE_COLUMNS, **KI_COLUMNS, **SYNONYM_COLUMNS, **VERB_COLUMNS}.keys()))
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


def update_google_sheet_row(df: pd.DataFrame, row_idx: int) -> None:
    """Aktualisiert nur eine einzelne Vokabelzeile in Google Sheets."""
    df_clean = ensure_columns(df.copy()).fillna("")

    if google_sheets_configured():
        ws = get_vocab_worksheet()
        headers = df_clean.columns.tolist()

        current_headers = ws.row_values(1)
        if current_headers != headers:
            ws.update("A1", [headers])

        google_row_number = int(row_idx) + 2
        row_values = df_clean.loc[row_idx, headers].astype(str).tolist()
        end_cell = gspread.utils.rowcol_to_a1(google_row_number, len(headers))
        cell_range = f"A{google_row_number}:{end_cell}"
        ws.update(cell_range, [row_values])
    else:
        ensure_excel_exists()
        df_clean.to_excel(EXCEL_PATH, index=False)

    st.cache_data.clear()


def mark_vocab_row_pending(df: pd.DataFrame, row_idx: int) -> None:
    """Merkt eine geänderte Vokabelzeile für spätere Synchronisierung."""
    if "pending_vocab_updates" not in st.session_state:
        st.session_state.pending_vocab_updates = {}

    word_id = str(df.at[row_idx, "ID"])
    st.session_state.pending_vocab_updates[word_id] = df.loc[row_idx].to_dict()


def apply_pending_vocab_updates_to_df(df: pd.DataFrame) -> pd.DataFrame:
    """Überträgt noch nicht synchronisierte Session-Änderungen in den aktuell geladenen DataFrame."""
    pending = st.session_state.get("pending_vocab_updates", {})
    if not pending or df.empty or "ID" not in df.columns:
        return df

    df = df.copy()
    for word_id, row_data in pending.items():
        matches = df.index[df["ID"].astype(str) == str(word_id)].tolist()
        if not matches:
            continue
        idx = matches[0]
        for col, value in row_data.items():
            if col in df.columns:
                df.at[idx, col] = value
    return ensure_columns(df)


def flush_pending_vocab_updates(df: pd.DataFrame) -> int:
    """Schreibt alle gepufferten Vokabeländerungen nach Google Sheets."""
    pending = st.session_state.get("pending_vocab_updates", {})
    if not pending:
        return 0

    df_work = apply_pending_vocab_updates_to_df(df.copy())
    count = 0

    for word_id in list(pending.keys()):
        idx = find_row_index(df_work, word_id)
        if idx is not None:
            update_google_sheet_row(df_work, idx)
            count += 1

    st.session_state.pending_vocab_updates = {}
    st.session_state.pending_answer_count = 0
    st.cache_data.clear()
    return count


def append_vocab_rows(df: pd.DataFrame, rows_to_add: list[dict]) -> pd.DataFrame:
    """Hängt neue Vokabelzeilen effizient an Google Sheets an und gibt den aktualisierten DataFrame zurück."""
    if not rows_to_add:
        return df

    df_new = ensure_columns(pd.concat([df, pd.DataFrame(rows_to_add)], ignore_index=True))

    if google_sheets_configured():
        ws = get_vocab_worksheet()
        headers = df_new.columns.tolist()
        current_headers = ws.row_values(1)
        if current_headers != headers:
            ws.update("A1", [headers])

        add_df = ensure_columns(pd.DataFrame(rows_to_add))
        for col in headers:
            if col not in add_df.columns:
                add_df[col] = ""
        ws.append_rows(add_df[headers].fillna("").astype(str).values.tolist(), value_input_option="USER_ENTERED")
    else:
        save_data(df_new)

    st.cache_data.clear()
    return df_new


def find_row_index(df: pd.DataFrame, word_id: str) -> int | None:
    matches = df.index[df["ID"].astype(str) == str(word_id)].tolist()
    return matches[0] if matches else None


# ============================================================
# Hilfsfunktionen: Lernhistorie / Tagesziel / Streak
# ============================================================

def get_or_create_worksheet(sheet_name: str, headers: list[str], rows: int = 1000, cols: int = 30):
    """Holt oder erstellt ein zusätzliches Google-Sheet-Worksheet."""
    sheet_id = get_google_sheet_id()
    if not sheet_id:
        return None

    gc = get_google_client()
    sh = gc.open_by_key(sheet_id)

    try:
        ws = sh.worksheet(sheet_name)
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=sheet_name, rows=rows, cols=cols)
        ws.update([headers])
        return ws

    values = ws.get_all_values()
    if not values:
        ws.update([headers])
    return ws

def get_history_worksheet():
    if not google_sheets_configured():
        return None
    return get_or_create_worksheet("Lernhistorie", HISTORY_COLUMNS, rows=3000, cols=len(HISTORY_COLUMNS) + 2)

def get_settings_worksheet():
    if not google_sheets_configured():
        return None
    return get_or_create_worksheet("Einstellungen", SETTINGS_COLUMNS, rows=100, cols=5)

@st.cache_data(show_spinner=False, ttl=60)
def load_history_cached(source_key: str) -> pd.DataFrame:
    if google_sheets_configured():
        ws = get_history_worksheet()
        if ws is None:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        values = ws.get_all_values()
        if not values:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        headers = values[0]
        rows = values[1:]
        if not headers:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        hist = pd.DataFrame(rows, columns=headers)
        for col in HISTORY_COLUMNS:
            if col not in hist.columns:
                hist[col] = ""
        return hist[HISTORY_COLUMNS].fillna("")

    path = "lernhistorie.csv"
    if os.path.exists(path):
        return pd.read_csv(path, dtype=str).fillna("")
    return pd.DataFrame(columns=HISTORY_COLUMNS)

def load_history() -> pd.DataFrame:
    if google_sheets_configured():
        source_key = f"history:{get_google_sheet_id()}:Lernhistorie"
    else:
        path = "lernhistorie.csv"
        source_key = f"history_local:{os.path.getmtime(path) if os.path.exists(path) else 0}"
    return load_history_cached(source_key).copy()

def build_learning_event_row(
    word_id: str,
    deutsch: str,
    englisch: str,
    kategorie: str,
    modus: str,
    antwort: str,
    erwartete_antwort: str,
    korrekt: bool,
    hinweis: str = "",
) -> list[str]:
    now = datetime.now()
    return [
        now.strftime("%Y-%m-%d %H:%M:%S"),
        now.strftime("%Y-%m-%d"),
        str(word_id),
        str(deutsch),
        str(englisch),
        str(kategorie),
        str(modus),
        str(antwort),
        str(erwartete_antwort),
        "TRUE" if korrekt else "FALSE",
        str(hinweis),
    ]


def flush_pending_history() -> int:
    """Schreibt gepufferte Lernhistorie gesammelt nach Google Sheets bzw. lokal in CSV."""
    pending = st.session_state.get("pending_history_rows", [])
    if not pending:
        return 0

    if google_sheets_configured():
        ws = get_history_worksheet()
        ws.append_rows(pending, value_input_option="USER_ENTERED")
    else:
        path = "lernhistorie.csv"
        hist = load_history()
        add_df = pd.DataFrame([dict(zip(HISTORY_COLUMNS, row)) for row in pending])
        hist = pd.concat([hist, add_df], ignore_index=True)
        hist.to_csv(path, index=False)

    count = len(pending)
    st.session_state.pending_history_rows = []
    st.cache_data.clear()
    return count


def append_learning_event(
    word_id: str,
    deutsch: str,
    englisch: str,
    kategorie: str,
    modus: str,
    antwort: str,
    erwartete_antwort: str,
    korrekt: bool,
    hinweis: str = "",
) -> None:
    """Puffert eine Antwort in der Lernhistorie. Synchronisiert gesammelt statt sofort pro Antwort."""
    if "pending_history_rows" not in st.session_state:
        st.session_state.pending_history_rows = []

    row_values = build_learning_event_row(
        word_id=word_id,
        deutsch=deutsch,
        englisch=englisch,
        kategorie=kategorie,
        modus=modus,
        antwort=antwort,
        erwartete_antwort=erwartete_antwort,
        korrekt=korrekt,
        hinweis=hinweis,
    )

    st.session_state.pending_history_rows.append(row_values)


def maybe_auto_sync_pending(df: pd.DataFrame) -> tuple[int, int]:
    """Synchronisiert automatisch, sobald genug Antworten gepuffert sind."""
    if st.session_state.get("pending_answer_count", 0) < SYNC_BATCH_SIZE:
        return 0, 0
    vocab_count = flush_pending_vocab_updates(df)
    history_count = flush_pending_history()
    return vocab_count, history_count


def get_setting_value(key: str, default: str = "") -> str:
    if google_sheets_configured():
        try:
            ws = get_settings_worksheet()
            values = ws.get_all_records()
            for item in values:
                if str(item.get("Key", "")).strip() == key:
                    return str(item.get("Value", default)).strip()
        except Exception:
            return default
    return default

def set_setting_value(key: str, value: str) -> None:
    if not google_sheets_configured():
        return
    ws = get_settings_worksheet()
    values = ws.get_all_values()
    if not values:
        ws.update([SETTINGS_COLUMNS])
        values = [SETTINGS_COLUMNS]

    for row_idx, row_values in enumerate(values[1:], start=2):
        if len(row_values) >= 1 and str(row_values[0]).strip() == key:
            ws.update_cell(row_idx, 2, str(value))
            st.cache_data.clear()
            return

    ws.append_row([key, str(value)], value_input_option="USER_ENTERED")
    st.cache_data.clear()

def calculate_current_streak(history: pd.DataFrame) -> int:
    """Berechnet die aktuelle Lernserie in Tagen, endend heute."""
    if history.empty or "Datum" not in history.columns:
        return 0

    dates = pd.to_datetime(history["Datum"], errors="coerce").dropna().dt.normalize()
    if dates.empty:
        return 0

    learned_dates = set(dates.dt.date)
    day = pd.Timestamp.today().normalize().date()
    streak = 0

    while day in learned_dates:
        streak += 1
        day = (pd.Timestamp(day) - pd.Timedelta(days=1)).date()

    return streak

def history_summary(history: pd.DataFrame, daily_goal: int) -> dict:
    if history.empty:
        return {"today_answers": 0, "today_correct": 0, "today_words": 0, "streak": 0, "goal_progress": 0.0}

    hist = history.copy()
    hist["Datum_dt"] = pd.to_datetime(hist.get("Datum", ""), errors="coerce")
    hist["Korrekt_bool"] = hist.get("Korrekt", "").astype(str).str.lower().isin(["true", "1", "ja", "yes"])
    today = pd.Timestamp.today().normalize()
    today_hist = hist[hist["Datum_dt"].dt.normalize() == today]

    today_answers = len(today_hist)
    today_correct = int(today_hist["Korrekt_bool"].sum()) if not today_hist.empty else 0
    today_words = int(today_hist["ID"].astype(str).nunique()) if not today_hist.empty and "ID" in today_hist.columns else 0
    return {
        "today_answers": today_answers,
        "today_correct": today_correct,
        "today_words": today_words,
        "streak": calculate_current_streak(hist),
        "goal_progress": min(today_answers / max(int(daily_goal), 1), 1.0),
    }







def render_sync_status(df: pd.DataFrame, location: str = "") -> None:
    """Zeigt ausstehende lokale Änderungen und bietet manuelle Synchronisierung an."""
    pending_vocab = len(st.session_state.get("pending_vocab_updates", {}))
    pending_hist = len(st.session_state.get("pending_history_rows", []))

    if pending_vocab == 0 and pending_hist == 0:
        return

    st.warning(
        f"⏳ Noch nicht synchronisiert: {pending_vocab} Vokabeländerung(en), "
        f"{pending_hist} Historieneintrag/Einträge."
    )
    if st.button("🔄 Änderungen jetzt synchronisieren", key=f"sync_now_{location}"):
        saved_vocab = flush_pending_vocab_updates(df)
        saved_hist = flush_pending_history()
        st.success(f"Synchronisiert: {saved_vocab} Vokabelzeile(n), {saved_hist} Historieneintrag/Einträge.")
        st.rerun()


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



def generate_ai_cloze_sentence(word_de: str, word_en: str, category: str, level: str = "mittel", variant: int = 0):
    """Erzeugt einen gezielten, abwechslungsreichen Lückentext für die aktuelle Vokabel."""
    api_key = get_openai_api_key()
    if not api_key:
        return {"ok": False, "error": "OPENAI_API_KEY fehlt.", "data": {}}

    client = OpenAI(api_key=api_key)
    prompt = f"""
Du bist ein Englischlehrer für einen deutschen Lerner.
Erstelle genau einen hochwertigen englischen Lückentext für einen Vokabeltrainer.

Deutsch: {word_de}
Englisch: {word_en}
Kategorie: {category}
Schwierigkeit: {level}
Variante: {variant}

Ziel:
- Der Satz soll natürlich klingen und nicht generisch sein.
- Der Satz soll zur Kategorie passen.
- Die Lösung soll die gesuchte Vokabel oder eine grammatisch notwendige Form davon sein.
- Wenn die Vokabel ein Verb ist, darfst du eine passende konjugierte Form verwenden, z. B. "hedges", "hedged", "is hedging".
- Verwende genau eine Lücke mit fünf Unterstrichen: _____
- Der vollständige Satz muss die Lösung enthalten.
- Keine Markdown-Ausgabe, ausschließlich JSON.

JSON-Format:
{{
  "cloze_sentence": "... _____ ...",
  "answer": "...",
  "full_sentence_en": "...",
  "translation_de": "...",
  "hint_de": "kurzer Hinweis auf Deutsch, ohne die Lösung direkt zu verraten"
}}
"""
    try:
        response = client.responses.create(model=get_openai_model(), input=prompt)
        data = extract_json(response.output_text)
        result = {
            "cloze_sentence": str(data.get("cloze_sentence", "")).strip(),
            "answer": str(data.get("answer", "")).strip(),
            "full_sentence_en": str(data.get("full_sentence_en", "")).strip(),
            "translation_de": str(data.get("translation_de", "")).strip(),
            "hint_de": str(data.get("hint_de", "")).strip(),
        }
        if "_____" not in result["cloze_sentence"] or not result["answer"]:
            raise ValueError("Die KI-Antwort enthält keinen gültigen Lückensatz.")
        return {"ok": True, "error": "", "data": result}
    except Exception as e:
        return {"ok": False, "error": str(e), "data": {}}




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



def generate_ai_verb_forms(word_de: str, word_en: str, category: str, level: str = "mittel", variant: int = 0):
    """Erzeugt Verbformen und Zeitformen für englische Verben."""
    api_key = get_openai_api_key()
    if not api_key:
        return {"ok": False, "error": "OPENAI_API_KEY fehlt.", "data": {}}

    client = OpenAI(api_key=api_key)
    prompt = f"""
Du bist ein Englischlehrer für deutsche Lernende.
Prüfe, ob die englische Vokabel ein Verb ist. Falls ja, erstelle Verbformen und wichtige Zeitformen.

Deutsch: {word_de}
Englisch: {word_en}
Kategorie: {category}
Schwierigkeit: {level}
Variante: {variant}

Regeln:
- Falls die englische Vokabel kein Verb ist, gib "is_verb": false zurück.
- Falls es ein Verb ist, gib "is_verb": true zurück.
- Nutze korrektes Standardenglisch.
- Gib die Formen knapp und eindeutig aus.
- "you_plural" steht für "you" im Plural.
- Gib eine kurze deutsche Notiz, insbesondere bei unregelmäßigen Verben oder Verben mit Präposition.
- Antworte ausschließlich als valides JSON ohne Markdown.

JSON-Format:
{{
  "is_verb": true,
  "infinitive": "to ...",
  "base_form": "...",
  "third_person_singular": "...",
  "past_simple": "...",
  "past_participle": "...",
  "ing_form": "...",
  "regularity": "regular|irregular|mixed|not_applicable",
  "note_de": "...",
  "tenses": {{
    "present_simple": {{"I": "...", "you": "...", "he/she/it": "...", "we": "...", "you_plural": "...", "they": "..."}},
    "present_continuous": {{"I": "...", "you": "...", "he/she/it": "...", "we": "...", "you_plural": "...", "they": "..."}},
    "past_simple": {{"I": "...", "you": "...", "he/she/it": "...", "we": "...", "you_plural": "...", "they": "..."}},
    "past_continuous": {{"I": "...", "you": "...", "he/she/it": "...", "we": "...", "you_plural": "...", "they": "..."}},
    "present_perfect": {{"I": "...", "you": "...", "he/she/it": "...", "we": "...", "you_plural": "...", "they": "..."}},
    "past_perfect": {{"I": "...", "you": "...", "he/she/it": "...", "we": "...", "you_plural": "...", "they": "..."}},
    "future_simple": {{"I": "...", "you": "...", "he/she/it": "...", "we": "...", "you_plural": "...", "they": "..."}},
    "conditional": {{"I": "...", "you": "...", "he/she/it": "...", "we": "...", "you_plural": "...", "they": "..."}}
  }},
  "example_sentences": [
    {{"tense": "present_simple", "en": "...", "de": "..."}},
    {{"tense": "past_simple", "en": "...", "de": "..."}},
    {{"tense": "present_perfect", "en": "...", "de": "..."}}
  ]
}}
"""
    try:
        response = client.responses.create(model=get_openai_model(), input=prompt)
        data = extract_json(response.output_text)
        if not isinstance(data, dict):
            raise ValueError("Die KI-Antwort war kein JSON-Objekt.")
        return {"ok": True, "error": "", "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e), "data": {}}


def parse_verb_forms_json(value):
    """Liest gespeicherte Verbformen aus einer JSON-Spalte."""
    if value is None or pd.isna(value) or not str(value).strip():
        return None
    try:
        data = json.loads(str(value))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def render_verb_forms(verb_data: dict):
    """Stellt Verbformen übersichtlich in Streamlit dar."""
    if not verb_data:
        return

    if not verb_data.get("is_verb", False):
        st.info("Diese Vokabel wurde von der KI nicht als Verb erkannt.")
        note = str(verb_data.get("note_de", "")).strip()
        if note:
            st.caption(note)
        return

    note = str(verb_data.get("note_de", "")).strip()
    if note:
        st.info(note)

    basis_df = pd.DataFrame([
        {"Form": "Infinitive", "Wert": verb_data.get("infinitive", "")},
        {"Form": "Base form", "Wert": verb_data.get("base_form", "")},
        {"Form": "3rd person singular", "Wert": verb_data.get("third_person_singular", "")},
        {"Form": "Past simple", "Wert": verb_data.get("past_simple", "")},
        {"Form": "Past participle", "Wert": verb_data.get("past_participle", "")},
        {"Form": "-ing form", "Wert": verb_data.get("ing_form", "")},
        {"Form": "Regularity", "Wert": verb_data.get("regularity", "")},
    ])
    st.markdown("#### Basisformen")
    st.dataframe(basis_df, use_container_width=True, hide_index=True)

    tenses = verb_data.get("tenses", {})
    if isinstance(tenses, dict) and tenses:
        st.markdown("#### Zeitformen")
        tense_labels = {
            "present_simple": "Present Simple",
            "present_continuous": "Present Continuous",
            "past_simple": "Past Simple",
            "past_continuous": "Past Continuous",
            "present_perfect": "Present Perfect",
            "past_perfect": "Past Perfect",
            "future_simple": "Future Simple",
            "conditional": "Conditional",
        }

        tabs = st.tabs([tense_labels.get(k, str(k).replace("_", " ").title()) for k in tenses.keys()])
        for tab, (tense_key, forms) in zip(tabs, tenses.items()):
            with tab:
                if isinstance(forms, dict):
                    tense_df = pd.DataFrame(
                        [{"Person": person, "Form": form} for person, form in forms.items()]
                    )
                    st.dataframe(tense_df, use_container_width=True, hide_index=True)

    examples = verb_data.get("example_sentences", [])
    if isinstance(examples, list) and examples:
        st.markdown("#### Beispielsätze zu Zeitformen")
        ex_rows = []
        for ex in examples:
            if isinstance(ex, dict):
                ex_rows.append({
                    "Zeitform": str(ex.get("tense", "")).replace("_", " ").title(),
                    "Englisch": ex.get("en", ""),
                    "Deutsch": ex.get("de", ""),
                })
        if ex_rows:
            st.dataframe(pd.DataFrame(ex_rows), use_container_width=True, hide_index=True)





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


def build_new_word_id(df: pd.DataFrame, offset: int = 1) -> int:
    """Erzeugt eine einfache fortlaufende numerische ID."""
    if "ID" not in df.columns or df.empty:
        return offset

    numeric_ids = pd.to_numeric(df["ID"], errors="coerce")
    max_id = numeric_ids.max()

    if pd.isna(max_id):
        return offset

    return int(max_id) + offset

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

def get_next_numeric_id(df):
    """Ermittelt die nächste einfache numerische ID aus der bestehenden ID-Spalte."""
    if "ID" not in df.columns or df.empty:
        return 1

    numeric_ids = pd.to_numeric(df["ID"], errors="coerce")
    max_id = numeric_ids.max()

    if pd.isna(max_id):
        return 1

    return int(max_id) + 1

def assign_missing_numeric_ids(df):
    """
    Vergibt einfache fortlaufende IDs für Zeilen, bei denen ID leer oder ungültig ist.
    Bestehende numerische IDs bleiben unverändert.
    """
    df = df.copy()

    if "ID" not in df.columns:
        df["ID"] = ""

    numeric_ids = pd.to_numeric(df["ID"], errors="coerce")
    max_id = numeric_ids.max()

    if pd.isna(max_id):
        next_id = 1
    else:
        next_id = int(max_id) + 1

    for idx in df.index:
        current_id = pd.to_numeric(df.at[idx, "ID"], errors="coerce")

        if pd.isna(current_id):
            df.at[idx, "ID"] = next_id
            next_id += 1
        else:
            df.at[idx, "ID"] = int(current_id)

    return df

# ============================================================
# UI Setup
# ============================================================
st.set_page_config(page_title="Vokabeltrainer", page_icon="📘", layout="wide")
st.title("📘 Intelligenter Vokabeltrainer")
st.caption("Mit KI-Beispielsätzen, Lernpriorisierung, Tests, Dashboard, Admin-Bereich und Google-Sheets-Speicherung")

st.markdown("""
<style>
.home-card {
    border: 1px solid rgba(49, 51, 63, 0.12);
    border-radius: 18px;
    padding: 1.1rem 1.2rem;
    background: linear-gradient(135deg, rgba(53,211,223,0.10), rgba(255,255,255,0.02));
    box-shadow: 0 8px 22px rgba(0,0,0,0.04);
    min-height: 125px;
}
.home-card h3 {
    margin: 0 0 0.35rem 0;
    font-size: 1.05rem;
}
.home-card .big {
    font-size: 2.0rem;
    font-weight: 800;
    margin: 0.25rem 0;
}
.home-card .small {
    opacity: 0.75;
    font-size: 0.92rem;
}
.focus-box {
    border-left: 5px solid #35D3DF;
    padding: 0.9rem 1rem;
    border-radius: 12px;
    background: rgba(53,211,223,0.08);
}
</style>
""", unsafe_allow_html=True)

# Daten laden
df = load_data()
learning_history = load_history()

try:
    daily_goal_default = int(get_setting_value("daily_goal", "20"))
except Exception:
    daily_goal_default = 20

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
st.session_state.setdefault("last_ai_verbforms", None)
st.session_state.setdefault("last_ai_cloze", None)
st.session_state.setdefault("cloze_refresh", 0)
st.session_state.setdefault("last_generated_vocabulary", [])
st.session_state.setdefault("vocab_generator_refresh", 0)
st.session_state.setdefault("ai_refresh", 0)
st.session_state.setdefault("synonym_refresh", 0)
st.session_state.setdefault("test_aktiv", False)
st.session_state.setdefault("test_vokabeln", None)
st.session_state.setdefault("test_index", 0)
st.session_state.setdefault("test_ergebnisse", [])
st.session_state.setdefault("test_direction", "Deutsch → Englisch")
st.session_state.setdefault("pending_vocab_updates", {})
st.session_state.setdefault("pending_history_rows", [])
st.session_state.setdefault("pending_answer_count", 0)

# Noch nicht synchronisierte Änderungen in den geladenen DataFrame einblenden
df = apply_pending_vocab_updates_to_df(df)

tab_home, tab_training, tab_test, tab_dashboard, tab_admin, tab_generator, tab_settings = st.tabs([
    "🏠 Start",
    "🏋️ Training",
    "🎓 Test",
    "📊 Dashboard",
    "🛠️ Admin",
    "➕ KI-Vokabelgenerator",
    "⚙️ Einstellungen",
])


# ============================================================
# Startseite
# ============================================================
with tab_home:
    st.header("🏠 Willkommen zurück")
    render_sync_status(df, "home")

    if df.empty:
        st.info("Noch keine Vokabeln vorhanden. Lege im Admin-Bereich deine ersten Vokabeln an oder nutze den KI-Vokabelgenerator.")
    else:
        home = df.copy()
        home["Richtig"] = pd.to_numeric(home["Richtig"], errors="coerce").fillna(0).astype(int)
        home["Falsch"] = pd.to_numeric(home["Falsch"], errors="coerce").fillna(0).astype(int)
        home["Total"] = home["Richtig"] + home["Falsch"]
        total_words = len(home)
        total_answers = int(home["Total"].sum())
        total_correct = int(home["Richtig"].sum())
        accuracy = (total_correct / max(total_answers, 1)) * 100
        trained_words = int((home["Total"] > 0).sum())

        history_stats = history_summary(learning_history, daily_goal_default)
        trained_today = history_stats["today_words"]
        answers_today = history_stats["today_answers"]
        correct_today = history_stats["today_correct"]
        streak_days = history_stats["streak"]
        goal_progress = history_stats["goal_progress"]

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.markdown(f"""<div class='home-card'><h3>📚 Vokabeln</h3><div class='big'>{total_words}</div><div class='small'>{trained_words} bereits trainiert</div></div>""", unsafe_allow_html=True)
        with c2:
            st.markdown(f"""<div class='home-card'><h3>✅ Trefferquote</h3><div class='big'>{accuracy:.0f}%</div><div class='small'>{total_answers} Antworten gesamt</div></div>""", unsafe_allow_html=True)
        with c3:
            st.markdown(f"""<div class='home-card'><h3>🔥 Heute</h3><div class='big'>{answers_today}</div><div class='small'>{trained_today} unterschiedliche Vokabeln</div></div>""", unsafe_allow_html=True)
        with c4:
            st.markdown(f"""<div class='home-card'><h3>🔥 Streak</h3><div class='big'>{streak_days}</div><div class='small'>Tage in Folge gelernt</div></div>""", unsafe_allow_html=True)

        st.markdown("---")
        goal_col1, goal_col2 = st.columns([1, 2])
        with goal_col1:
            new_daily_goal = st.number_input("🎯 Tagesziel Antworten", min_value=1, max_value=200, value=int(daily_goal_default), step=1)
            if st.button("Tagesziel speichern"):
                set_setting_value("daily_goal", str(int(new_daily_goal)))
                st.success("Tagesziel gespeichert.")
                st.rerun()
        with goal_col2:
            st.markdown(f"**Fortschritt heute:** {answers_today} / {daily_goal_default} Antworten")
            st.progress(goal_progress)
            today_accuracy = (correct_today / max(answers_today, 1)) * 100
            st.caption(f"Heute richtig: {correct_today} von {answers_today} ({today_accuracy:.0f} %) · Streak: {streak_days} Tage")

        difficult_count = int(((home["Falsch"] > home["Richtig"]) & (home["Total"] > 0)).sum())

        st.markdown("---")
        left, right = st.columns([1.2, 1])

        with left:
            st.markdown("### 🎯 Empfehlung für die nächste Session")
            focus = home[home["Total"] > 0].copy()
            if not focus.empty:
                focus["Fehlerquote"] = focus["Falsch"] / focus["Total"].replace(0, pd.NA)
                focus = focus.sort_values(["Fehlerquote", "Falsch", "Total"], ascending=False).head(8)
                st.dataframe(
                    focus[["Deutsch", "Englisch", "Kategorie", "Richtig", "Falsch"]],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("Starte mit einer kurzen Trainingsrunde. Danach erscheinen hier automatisch deine schwierigsten Wörter.")

        with right:
            st.markdown("### 🚀 Schnellstart")
            st.markdown(
                """
<div class='focus-box'>
<b>Empfohlener Ablauf</b><br><br>
1. Im Tab <b>Training</b> eine Kategorie wählen<br>
2. 10–15 Vokabeln üben<br>
3. Danach im <b>Dashboard</b> die schwierigsten Wörter prüfen<br>
4. Bei Verben optional Verbformen erzeugen<br>
5. Neue Themen über den <b>KI-Vokabelgenerator</b> ergänzen
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("---")
        st.markdown("### 🧾 Lernhistorie zuletzt")
        if learning_history.empty:
            st.info("Noch keine Lernhistorie vorhanden. Sobald du trainierst oder Tests machst, wird hier automatisch protokolliert.")
        else:
            hist_recent = learning_history.tail(10).copy()
            if "Korrekt" in hist_recent.columns:
                hist_recent["Ergebnis"] = hist_recent["Korrekt"].astype(str).str.lower().map(lambda x: "✅" if x in ["true", "1", "ja", "yes"] else "❌")
            cols = [c for c in ["Zeitstempel", "Ergebnis", "Deutsch", "Englisch", "Kategorie", "Modus", "Antwort"] if c in hist_recent.columns]
            st.dataframe(hist_recent[cols].iloc[::-1], use_container_width=True, hide_index=True)

        st.markdown("---")
        st.markdown("### 📊 Kategorienüberblick")
        cat_overview = home.groupby("Kategorie", dropna=False).agg(
            Vokabeln=("ID", "count"),
            Richtig=("Richtig", "sum"),
            Falsch=("Falsch", "sum"),
        ).reset_index()
        cat_overview["Antworten"] = cat_overview["Richtig"] + cat_overview["Falsch"]
        cat_overview["Trefferquote_%"] = (cat_overview["Richtig"] / cat_overview["Antworten"].replace(0, pd.NA) * 100).fillna(0).round(0).astype(int)
        st.dataframe(cat_overview.sort_values("Vokabeln", ascending=False), use_container_width=True, hide_index=True)

# ============================================================
# Training
# ============================================================
with tab_training:
    st.header("🏋️‍♂️ Training")
    render_sync_status(df, "training")

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
        st.session_state.last_ai_cloze = None
        st.session_state.ai_refresh += 1
        st.session_state.synonym_refresh += 1
        st.session_state.cloze_refresh += 1

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
        st.subheader("Fülle die Lücke:")
        st.caption("Für bessere Qualität kann die KI gezielt einen neuen Lückensatz erzeugen. Gespeicherte Sätze werden nur als günstiger Fallback genutzt.")

        cloze_key = f"ai_cloze_{st.session_state.current_word_id}"
        generate_cloze = st.button("🤖 Neuen KI-Lückentext erzeugen", key=f"generate_cloze_{st.session_state.current_word_id}")

        if generate_cloze:
            with st.spinner("KI erstellt einen passenden Lückentext ..."):
                cloze_result = generate_ai_cloze_sentence(
                    word_de=vokabel_de,
                    word_en=vokabel_en,
                    category=selected_category,
                    level=level_default,
                    variant=st.session_state.cloze_refresh,
                )
            st.session_state.cloze_refresh += 1
            if cloze_result["ok"]:
                st.session_state[cloze_key] = cloze_result["data"]
                st.session_state.last_ai_cloze = {"word_id": st.session_state.current_word_id, **cloze_result["data"]}
            else:
                st.warning(f"Lückentext konnte nicht erzeugt werden: {cloze_result['error']}")

        cloze_data = st.session_state.get(cloze_key)

        if cloze_data:
            st.info(cloze_data.get("cloze_sentence", ""))
            if cloze_data.get("hint_de"):
                st.caption(f"Hinweis: {cloze_data.get('hint_de')}")
            with st.expander("Lösung / vollständiger Satz anzeigen"):
                st.success(cloze_data.get("full_sentence_en", ""))
                if cloze_data.get("translation_de"):
                    st.write(cloze_data.get("translation_de"))
            expected_answer = cloze_data.get("answer", vokabel_en) or vokabel_en
            accepted_for_mode = "; ".join([x for x in [accepted_answers, vokabel_en] if str(x).strip()])
        else:
            saved_sentence_candidates = []
            for i in range(1, 4):
                candidate = str(row.get(f"KI_EN_{i}", "")).strip()
                if candidate:
                    saved_sentence_candidates.append(candidate)

            fallback_key = f"fallback_cloze_{st.session_state.current_word_id}_{st.session_state.cloze_refresh}"
            if fallback_key not in st.session_state:
                if saved_sentence_candidates:
                    st.session_state[fallback_key] = random.choice(saved_sentence_candidates)
                else:
                    st.session_state[fallback_key] = f"I need to use the word {vokabel_en} correctly."

            base_sentence = st.session_state[fallback_key]
            st.info(create_cloze_sentence(base_sentence, vokabel_en))
            st.caption("Fallback aus gespeicherten Sätzen. Für mehr Abwechslung: KI-Lückentext erzeugen.")
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
            mark_vocab_row_pending(df, idx)
            st.session_state.pending_answer_count += 1
            append_learning_event(
                word_id=st.session_state.current_word_id,
                deutsch=vokabel_de,
                englisch=vokabel_en,
                kategorie=selected_category,
                modus=mode,
                antwort=user_input,
                erwartete_antwort=expected_answer,
                korrekt=correct,
                hinweis=reason,
            )
            maybe_auto_sync_pending(df)

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
                        update_google_sheet_row(df, idx)
                        st.success("Synonyme wurden in Google Sheets gespeichert.")
                        st.rerun()
            with save_syn_col2:
                if st.button("➕ Zu alternativen Antworten hinzufügen", use_container_width=True):
                    idx = find_row_index(df, st.session_state.current_word_id)
                    if idx is not None:
                        existing = str(df.at[idx, "Alternative_Antworten"] or "").strip()
                        additions = "; ".join(syn_data.get("synonyme_en", []))
                        df.at[idx, "Alternative_Antworten"] = "; ".join([x for x in [existing, additions] if x])
                        update_google_sheet_row(df, idx)
                        st.success("Synonyme wurden zusätzlich zu Alternative_Antworten hinzugefügt.")
                        st.rerun()

    # Verbformen & Zeitformen
    st.markdown("---")
    st.markdown("### 🔤 Verbformen & Zeitformen")
    st.caption("Für englische Verben kannst du Basisformen, Konjugationen und wichtige Zeitformen per KI erzeugen und in Google Sheets speichern.")

    saved_verb_data = parse_verb_forms_json(row.get("Verbformen_JSON", ""))
    if saved_verb_data:
        with st.expander("📌 Gespeicherte Verbformen anzeigen", expanded=False):
            render_verb_forms(saved_verb_data)

    vf_col1, vf_col2, vf_col3 = st.columns([1, 1, 2])
    with vf_col1:
        generate_verb_clicked = st.button("🤖 Verbformen erzeugen", use_container_width=True, key=f"generate_verbforms_{st.session_state.current_word_id}")
    with vf_col2:
        clear_verb_clicked = st.button("🧹 Anzeige leeren", use_container_width=True, key=f"clear_verbforms_{st.session_state.current_word_id}")
    with vf_col3:
        st.caption("Tipp: Besonders hilfreich bei unregelmäßigen Verben wie go/went/gone oder Verben mit Präpositionen.")

    if clear_verb_clicked:
        st.session_state.last_ai_verbforms = None
        st.rerun()

    if generate_verb_clicked:
        with st.spinner("KI erzeugt Verbformen und Zeitformen ..."):
            vf_result = generate_ai_verb_forms(
                word_de=vokabel_de,
                word_en=vokabel_en,
                category=selected_category,
                level=level_default,
                variant=st.session_state.ai_refresh,
            )
        st.session_state.ai_refresh += 1
        if vf_result["ok"]:
            st.session_state.last_ai_verbforms = {
                "word_id": st.session_state.current_word_id,
                "data": vf_result["data"],
            }
        else:
            st.warning(f"Verbformen konnten nicht erzeugt werden: {vf_result['error']}")

    vf_state = st.session_state.last_ai_verbforms
    if vf_state and vf_state.get("word_id") == st.session_state.current_word_id:
        verb_data = vf_state.get("data", {})
        with st.container(border=True):
            st.markdown("**KI-Vorschlag für diese Vokabel**")
            render_verb_forms(verb_data)

            if st.button("💾 Verbformen speichern", use_container_width=True, key=f"save_verbforms_{st.session_state.current_word_id}"):
                idx = find_row_index(df, st.session_state.current_word_id)
                if idx is not None:
                    df.at[idx, "Wortart"] = "verb" if verb_data.get("is_verb", False) else "kein Verb"
                    df.at[idx, "Verbformen_JSON"] = json.dumps(verb_data, ensure_ascii=False)
                    df.at[idx, "Verbformen_Notiz"] = str(verb_data.get("note_de", "")).strip()
                    update_google_sheet_row(df, idx)
                    st.success("Verbformen wurden in Google Sheets gespeichert.")
                    st.rerun()
                else:
                    st.error("Vokabel konnte nicht in der Tabelle gefunden werden.")

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
                            update_google_sheet_row(df, idx)
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
                    mark_vocab_row_pending(df, real_idx)
                    st.session_state.pending_answer_count += 1
                    append_learning_event(
                        word_id=str(row_t["ID"]),
                        deutsch=str(row_t["Deutsch"]),
                        englisch=str(row_t["Englisch"]),
                        kategorie=str(row_t.get("Kategorie", "")),
                        modus=f"Test: {current_test_direction}",
                        antwort=user_input,
                        erwartete_antwort=expected_test_answer,
                        korrekt=correct,
                        hinweis=reason,
                    )
                    maybe_auto_sync_pending(df)
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

    st.markdown("### Lernaktivität")
    if learning_history.empty:
        st.info("Noch keine Lernhistorie vorhanden.")
    else:
        hist_dash = learning_history.copy()
        hist_dash["Datum_dt"] = pd.to_datetime(hist_dash.get("Datum", ""), errors="coerce")
        hist_dash = hist_dash.dropna(subset=["Datum_dt"])
        if hist_dash.empty:
            st.info("Noch keine auswertbaren Historien-Daten vorhanden.")
        else:
            hist_dash["Korrekt_bool"] = hist_dash.get("Korrekt", "").astype(str).str.lower().isin(["true", "1", "ja", "yes"])
            daily = hist_dash.groupby(hist_dash["Datum_dt"].dt.date).agg(
                Antworten=("ID", "count"),
                Vokabeln=("ID", "nunique"),
                Richtig=("Korrekt_bool", "sum"),
            ).reset_index().rename(columns={"Datum_dt": "Datum"})
            daily["Datum"] = pd.to_datetime(daily["Datum"])
            daily = daily.sort_values("Datum").tail(30)
            fig_hist = px.bar(
                daily,
                x="Datum",
                y="Antworten",
                hover_data={"Vokabeln": True, "Richtig": True, "Datum": "|%d.%m.%Y"},
                title="Antworten pro Tag",
            )
            fig_hist.update_traces(marker_color="#35D3DF", marker_line_width=0, opacity=0.9)
            fig_hist.update_layout(
                height=330,
                margin=dict(l=10, r=20, t=60, b=20),
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                showlegend=False,
                xaxis_title="",
                yaxis_title="Antworten",
            )
            st.plotly_chart(fig_hist, use_container_width=True)

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
                    new_id = get_next_numeric_id(df)

                    new_row = {**BASE_COLUMNS, **KI_COLUMNS, **SYNONYM_COLUMNS, **VERB_COLUMNS}
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

                    df = append_vocab_rows(df, [new_row])

                    st.success(f"Vokabel gespeichert mit ID {new_id}.")
                    st.rerun()

    st.markdown("### Daten bearbeiten")
    st.caption("Änderungen in dieser Tabelle werden nach Klick auf 'Änderungen speichern' in Google Sheets gespeichert.")

    edited = st.data_editor(
        df,
        num_rows="dynamic",
        use_container_width=True,
        key="data_editor"
    )

    if st.button("💾 Änderungen speichern"):
        edited = ensure_columns(edited.copy())
        edited = assign_missing_numeric_ids(edited)
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

                        new_row = {**BASE_COLUMNS, **KI_COLUMNS, **SYNONYM_COLUMNS, **VERB_COLUMNS}
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
                        df_new = append_vocab_rows(df, rows_to_add)
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
    render_sync_status(df, "settings")
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
plotly
""", language="text")
