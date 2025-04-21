import streamlit as st
import pandas as pd
import random
import matplotlib.pyplot as plt
import time

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

# ✅ Direktes Feedback nach der Eingabe
if st.session_state.antwort_gegeben:
    if st.session_state.antwort_richtig:
        st.success("✅ Deine Antwort ist korrekt!")
    else:
        st.error(f"❌ Leider falsch – richtig wäre: **{loesung}**")

# 📙 Deutsche Beispielsätze
st.markdown("### 🔴 Beispielsätze (Deutsch)")
for i in range(1, 4):
    satz = row.get(f"DE_{i}", "")
    if pd.notna(satz) and str(satz).strip() != "":
        st.info(f"- {satz}")

# 🔄 Button zum Anzeigen der englischen Sätze
if st.button("📘 Englische Sätze anzeigen"):
    st.session_state.zeige_englisch = True

if st.session_state.zeige_englisch:
    st.markdown("### 📘 Beispielsätze (Englisch)")
    for i in range(1, 4):
        satz = row.get(f"EN_{i}", "")
        if pd.notna(satz) and str(satz).strip() != "":
            st.success(f"- {satz}")

# 🔁 Button für nächste Vokabel direkt darunter
if st.button("➡️ Nächste Vokabel"):
    st.session_state.frage_index = random.randint(0, len(gefiltert) - 1)
    st.session_state.antwort_gegeben = False
    st.session_state.antwort_richtig = None
    st.session_state.zeige_englisch = False
    st.session_state.pop("antwort", None)  # Eingabefeld zurücksetzen
    st.rerun()

# 📊 Modernes Statistik-Diagramm zur aktuellen Vokabel
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
