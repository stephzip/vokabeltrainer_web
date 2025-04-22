import streamlit as st
import pandas as pd
import random
import matplotlib.pyplot as plt
import time
from gtts import gTTS
import os
import base64

# 📄 Excel-Datei laden
excel_path = "vokabeln.xlsx"
df = pd.read_excel(excel_path)

st.title("📘 Vokabeltrainer")

# 🎯 Kategorieauswahl
kategorien = df["Kategorie"].dropna().unique()
kategorie = st.selectbox("Kategorie auswählen:", kategorien)

# 🔍 Filter nach Kategorie
gefiltert = df[df["Kategorie"] == kategorie].reset_index(drop=True)

if gefiltert.empty:
    st.warning("⚠️ Keine Vokabeln in dieser Kategorie gefunden.")
    st.stop()

# 📄 Vokabelliste mit Anhören-Button
with st.expander("📄 Vokabelliste dieser Kategorie anzeigen"):
    for idx, row_ in gefiltert[["Deutsch", "Englisch"]].dropna().reset_index(drop=True).iterrows():
        col1, col2, col3 = st.columns([4, 4, 1])
        with col1:
            st.markdown(f"**🇩🇪 {row_['Deutsch']}**")
        with col2:
            st.markdown(f"**🇬🇧 {row_['Englisch']}**")
        with col3:
            if st.button("🔊", key=f"tts_{idx}"):
                tts = gTTS(text=row_["Englisch"], lang='en')
                filename = f"tts_output_{idx}.mp3"
                tts.save(filename)
                audio_file = open(filename, "rb")
                audio_bytes = audio_file.read()
                st.audio(audio_bytes, format='audio/mp3')
                audio_file.close()
                os.remove(filename)

# 💾 Session-Variablen
if "frage_index" not in st.session_state:
    st.session_state.frage_index = random.randint(0, len(gefiltert) - 1)
if "antwort_gegeben" not in st.session_state:
    st.session_state.antwort_gegeben = False
if "antwort_richtig" not in st.session_state:
    st.session_state.antwort_richtig = None
if "zeige_englisch" not in st.session_state:
    st.session_state.zeige_englisch = False
if "runde" not in st.session_state:
    st.session_state.runde = 0
if "wechsel_timer" not in st.session_state:
    st.session_state.wechsel_timer = None
if "abgefragt_kategorie" not in st.session_state:
    st.session_state.abgefragt_kategorie = {}

# 🔄 Fortschrittsliste für aktuelle Kategorie initialisieren
if kategorie not in st.session_state.abgefragt_kategorie:
    st.session_state.abgefragt_kategorie[kategorie] = set()

abgefragt_set = st.session_state.abgefragt_kategorie[kategorie]
abgefragt_anzahl = len(abgefragt_set)
gesamt_anzahl = len(gefiltert)
fortschritt = abgefragt_anzahl / gesamt_anzahl

# 📈 Fortschrittsanzeige
st.markdown("### 📈 Fortschritt")
st.progress(fortschritt)
st.text(f"{abgefragt_anzahl} von {gesamt_anzahl} Vokabeln in dieser Kategorie abgefragt")

if st.button("🔁 Fortschritt zurücksetzen"):
    st.session_state.abgefragt_kategorie[kategorie] = set()
    st.rerun()

# 👉 Aktuelle Frage
row = gefiltert.iloc[st.session_state.frage_index]
vokabel = row["Deutsch"]
loesung = str(row["Englisch"]).strip().lower()

# 🔎 Prüffunktion beim Enter-Druck
def antwort_pruefen():
    gegeben = st.session_state.get("antwort", "").strip().lower()
    st.session_state.antwort_gegeben = True
    st.session_state.antwort_richtig = (gegeben == loesung)

    idx_original = df[(df["Deutsch"] == vokabel) & (df["Kategorie"] == kategorie)].index
    if len(idx_original) > 0:
        idx = idx_original[0]
        if st.session_state.antwort_richtig:
            df.at[idx, "Richtig"] = df.at[idx, "Richtig"] + 1 if not pd.isna(df.at[idx, "Richtig"]) else 1
        else:
            df.at[idx, "Falsch"] = df.at[idx, "Falsch"] + 1 if not pd.isna(df.at[idx, "Falsch"]) else 1
        df.to_excel(excel_path, index=False)

    st.session_state.wechsel_timer = time.time()

# 🧾 Eingabefeld mit Enter-Erkennung
st.subheader(f"Übersetze: **{vokabel}**")

antwort_default = st.session_state.get("antwort", "")
antwort = st.text_input(
    "Englische Übersetzung eingeben:",
    value=antwort_default,
    key="antwort",
    on_change=antwort_pruefen
)

# 🔁 Button für nächste Vokabel direkt unter dem Eingabefeld
if st.button("➡️ Nächste Vokabel"):
    st.session_state.abgefragt_kategorie[kategorie].add(st.session_state.frage_index)
    st.session_state.frage_index = random.randint(0, len(gefiltert) - 1)
    st.session_state.antwort_gegeben = False
    st.session_state.antwort_richtig = None
    st.session_state.zeige_englisch = False
    st.session_state.pop("antwort", None)
    st.rerun()

# ✅ Direktes Feedback nach der Eingabe
if st.session_state.antwort_gegeben:
    if st.session_state.antwort_richtig:
        st.success("✅ Deine Antwort ist korrekt!")
    else:
        st.error(f"❌ Leider falsch – richtig wäre: **{loesung}**")

# 📙 Deutsche Beispielsätze mit individuellen Übersetzungs-Buttons
st.markdown("### 🔴 Beispielsätze (Deutsch)")

for i in range(1, 4):
    deutscher_satz = row.get(f"DE_{i}", "")
    englischer_satz = row.get(f"EN_{i}", "")

    if pd.notna(deutscher_satz) and str(deutscher_satz).strip() != "":
        with st.container():
            st.info(deutscher_satz)

            button_key = f"zeige_uebersetzung_{i}_{st.session_state.frage_index}"
            if st.button(f"💬 Übersetzung zu Satz {i} anzeigen", key=button_key):
                if pd.notna(englischer_satz) and str(englischer_satz).strip() != "":
                    st.success(englischer_satz)
                else:
                    st.warning("⚠️ Keine Übersetzung vorhanden.")

# 📊 Statistik zur aktuellen Vokabel
if st.session_state.antwort_gegeben:
    richtig = row["Richtig"] if not pd.isna(row["Richtig"]) else 0
    falsch = row["Falsch"] if not pd.isna(row["Falsch"]) else 0

    st.markdown("---")
    st.markdown("### 📊 Statistik zu dieser Vokabel")

    if richtig == 0 and falsch == 0:
        st.info("ℹ️ Zu dieser Vokabel gibt es noch keine Statistik.")
    else:
        fig, ax = plt.subplots(figsize=(3, 3))
        farben = ["#2ECC71", "#E74C3C"]

        wedges, texts, autotexts = ax.pie(
            [richtig, falsch],
            labels=["Richtig", "Falsch"],
            autopct="%1.1f%%",
            colors=farben,
            textprops={"fontsize": 10, "color": "black"},
            startangle=90
        )

        for autotext in autotexts:
            autotext.set_fontsize(10)
            autotext.set_color("white")

        ax.axis("equal")
        st.pyplot(fig)

