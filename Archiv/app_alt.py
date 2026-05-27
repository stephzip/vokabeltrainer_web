# python -m streamlit run app.py

import pandas as pd
import random
import matplotlib.pyplot as plt
import time
from gtts import gTTS
from io import BytesIO
import streamlit as st


# 📄 Excel-Datei laden
excel_path = "vokabeln.xlsx"
df = pd.read_excel(excel_path)

st.title("📘 Vokabeltrainer")
st.markdown("---")  # horizontale Linie

# 📦 Testmodus-Setup
if "test_aktiv" not in st.session_state:
    st.session_state.test_aktiv = False
if "test_vokabeln" not in st.session_state:
    st.session_state.test_vokabeln = None
if "test_kategorien" not in st.session_state:
    st.session_state.test_kategorien = []
if "test_index" not in st.session_state:
    st.session_state.test_index = 0
if "test_ergebnisse" not in st.session_state:
    st.session_state.test_ergebnisse = []
if "test_abgeschlossen" not in st.session_state:
    st.session_state.test_abgeschlossen = False

# 🎓 Testmodus
st.header("🎓 Test")

if not st.session_state.test_aktiv:
    test_kats = st.multiselect("Wähle die Kategorien für den Test:", df["Kategorie"].dropna().unique())

    if st.button("🎯 Neuer Test starten", disabled=len(test_kats) == 0):
        gefiltert_test = (
            df[df["Kategorie"].isin(test_kats)]
            .dropna(subset=["Deutsch", "Englisch"])
            .sample(n=min(25, len(df)), random_state=random.randint(0, 9999))
            .reset_index(drop=True)
        )
        st.session_state.test_aktiv = True
        st.session_state.test_kategorien = test_kats
        st.session_state.test_vokabeln = gefiltert_test
        st.session_state.test_index = 0
        st.session_state.test_ergebnisse = []
        st.session_state.test_abgeschlossen = False
        st.rerun()
else:
    st.success(f"✅ Test läuft mit Kategorien: {', '.join(st.session_state.test_kategorien)}")

    if st.button("🔄 Test zurücksetzen"):
        st.session_state.test_index = 0
        st.session_state.test_ergebnisse = []
        st.session_state.test_abgeschlossen = False
        st.rerun()

    if st.button("🆕 Neuer Test starten"):
        st.session_state.test_aktiv = False
        st.session_state.test_vokabeln = None
        st.session_state.test_kategorien = []
        st.session_state.test_index = 0
        st.session_state.test_ergebnisse = []
        st.session_state.test_abgeschlossen = False
        st.rerun()

    vokabel_df = st.session_state.test_vokabeln
    idx = st.session_state.test_index
    if idx < len(vokabel_df):
        row = vokabel_df.iloc[idx]
        st.subheader(f"Frage {idx+1}/25 – Übersetze: **{row['Deutsch']}**")
        user_input = st.text_input("Englische Übersetzung:", key=f"test_eingabe_{idx}")

        if st.button("Antwort prüfen", key=f"test_check_{idx}"):
            korrekt = user_input.strip().lower() == str(row["Englisch"]).strip().lower()
            st.session_state.test_vokabeln.at[idx, "User_Eingabe"] = user_input
            st.session_state.test_ergebnisse.append(korrekt)
            st.session_state.test_index += 1
            if st.session_state.test_index >= 25:
                st.session_state.test_abgeschlossen = True
            st.rerun()
    else:
        st.success("🎉 Test abgeschlossen!")
        richtig = sum(st.session_state.test_ergebnisse)
        falsch = len(st.session_state.test_ergebnisse) - richtig
        st.markdown(f"**Ergebnis: {richtig}/25 richtig!**")

        fig, ax = plt.subplots(figsize=(1, 1))
        wedges, _, autotexts = ax.pie(
            [richtig, falsch],
            labels=["Richtig", "Falsch"],
            autopct="%1.1f%%",
            colors=["#2ECC71", "#E74C3C"],
            startangle=90,
        )
        for autotext in autotexts:
            autotext.set_fontsize(10)
            autotext.set_color("white")
        ax.axis("equal")
        st.pyplot(fig)

        # 📝 Detailauswertung anzeigen
        st.markdown("### 📋 Detailauswertung")
        for i, korrekt in enumerate(st.session_state.test_ergebnisse):
            frage = st.session_state.test_vokabeln.iloc[i]
            symbol = "✅" if korrekt else "❌"
            farbe = "green" if korrekt else "red"
            eingabe = frage.get("User_Eingabe", "–")

            if korrekt:
                st.markdown(
                    f"<span style='color:{farbe}'>{symbol} {frage['Deutsch']} ➜ {frage['Englisch']}</span>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"<span style='color:{farbe}'>{symbol} {frage['Deutsch']} ➜ {frage['Englisch']}<br>"
                    f"<i>Deine Antwort:</i> <b>{eingabe}</b></span>",
                    unsafe_allow_html=True,
                )

# --------------------------------------------------------------------
st.markdown("---")
st.header("🏋️‍♂️ Training")

# 🎯 Kategorieauswahl
kategorien = df["Kategorie"].dropna().unique()
kategorie = st.selectbox("Kategorie auswählen:", kategorien)

# 🔍 Filter nach Kategorie
gefiltert = df[df["Kategorie"] == kategorie].reset_index(drop=True)

if gefiltert.empty:
    st.warning("⚠️ Keine Vokabeln in dieser Kategorie gefunden.")
    st.stop()

with st.expander("📄 Vokabelliste dieser Kategorie anzeigen"):
    for idx, row_ in gefiltert[["Deutsch", "Englisch"]].dropna().reset_index(drop=True).iterrows():
        col1, col2, col3 = st.columns([4, 4, 1])
        with col1:
            st.markdown(f"**🇩🇪 {row_['Deutsch']}**")
        with col2:
            st.markdown(f"**🇬🇧 {row_['Englisch']}**")
        with col3:
            if st.button("🔊", key=f"tts_{idx}"):
                tts = gTTS(text=row_["Englisch"], lang="en")
                mp3_fp = BytesIO()
                tts.write_to_fp(mp3_fp)
                mp3_fp.seek(0)
                st.audio(mp3_fp, format="audio/mp3", start_time=0)

# 🧠 Session-States fürs Training
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
if "reset_antwort" not in st.session_state:
    st.session_state.reset_antwort = False

if kategorie not in st.session_state.abgefragt_kategorie:
    st.session_state.abgefragt_kategorie[kategorie] = set()

abgefragt_set = st.session_state.abgefragt_kategorie[kategorie]
abgefragt_anzahl = len(abgefragt_set)
gesamt_anzahl = len(gefiltert)
fortschritt = abgefragt_anzahl / gesamt_anzahl

st.markdown("### 📈 Fortschritt")
st.progress(fortschritt)
st.text(f"{abgefragt_anzahl} von {gesamt_anzahl} Vokabeln in dieser Kategorie abgefragt")

if st.button("🔁 Fortschritt zurücksetzen"):
    st.session_state.abgefragt_kategorie[kategorie] = set()
    st.rerun()

# 🗣 Aktuelle Vokabel laden
if st.session_state.frage_index >= len(gefiltert):
    st.session_state.frage_index = 0
row = gefiltert.iloc[st.session_state.frage_index]
vokabel = row["Deutsch"]
loesung = str(row["Englisch"]).strip().lower()


# ✅ Antwort prüfen
def antwort_pruefen():
    gegeben = st.session_state.get("antwort", "").strip().lower()
    st.session_state.antwort_gegeben = True
    st.session_state.antwort_richtig = gegeben == loesung

    idx_original = df[(df["Deutsch"] == vokabel) & (df["Kategorie"] == kategorie)].index
    if len(idx_original) > 0:
        idx = idx_original[0]
        if st.session_state.antwort_richtig:
            df.at[idx, "Richtig"] = df.at[idx, "Richtig"] + 1 if not pd.isna(df.at[idx, "Richtig"]) else 1
        else:
            df.at[idx, "Falsch"] = df.at[idx, "Falsch"] + 1 if not pd.isna(df.at[idx, "Falsch"]) else 1
        df.to_excel(excel_path, index=False)

    st.session_state.wechsel_timer = time.time()


# 🔁 Eingabefeld-Reset (Variante 1)
if st.session_state.reset_antwort:
    st.session_state.antwort = ""
    st.session_state.reset_antwort = False

st.subheader(f"Übersetze: **{vokabel}**")
antwort = st.text_input(
    "Englische Übersetzung eingeben:",
    key="antwort",
    on_change=antwort_pruefen,
)

# ➡️ Nächste Vokabel
if st.button("➡️ Nächste Vokabel"):
    st.session_state.abgefragt_kategorie[kategorie].add(st.session_state.frage_index)
    st.session_state.frage_index = random.randint(0, len(gefiltert) - 1)
    st.session_state.antwort_gegeben = False
    st.session_state.antwort_richtig = None
    st.session_state.zeige_englisch = False
    st.session_state.reset_antwort = True  # <--- hier setzen wir das Reset-Flag
    st.rerun()

# 🟩 Ergebnis-Feedback
if st.session_state.antwort_gegeben:
    if st.session_state.antwort_richtig:
        st.success("✅ Deine Antwort ist korrekt!")
    else:
        st.error(f"❌ Leider falsch – richtig wäre: **{loesung}**")

# 🔴 Beispielsätze (Deutsch)
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

# 📊 Statistik
if st.session_state.antwort_gegeben:
    richtig = row["Richtig"] if not pd.isna(row["Richtig"]) else 0
    falsch = row["Falsch"] if not pd.isna(row["Falsch"]) else 0

    st.markdown("---")
    st.markdown("### 📊 Statistik zu dieser Vokabel")

    if richtig == 0 and falsch == 0:
        st.info("ℹ️ Zu dieser Vokabel gibt es noch keine Statistik.")
    else:
        fig, ax = plt.subplots(figsize=(3, 3))
        wedges, texts, autotexts = ax.pie(
            [richtig, falsch],
            labels=["Richtig", "Falsch"],
            autopct="%1.1f%%",
            colors=["#2ECC71", "#E74C3C"],
            textprops={"fontsize": 10, "color": "black"},
            startangle=90,
        )
        for autotext in autotexts:
            autotext.set_fontsize(10)
            autotext.set_color("white")

        ax.axis("equal")
        st.pyplot(fig)
