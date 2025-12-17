# 1. Import des modules principaux
# Bibliothèques nécessaires au traitement des données, au traitement du langage, aux graphiques et à l interface utilisateur
import os
import os as _os
import time as time
import time as _time
import re
import json
import numpy as np
import pandas as pd
import gc
import matplotlib.pyplot as plt
import matplotlib.pyplot as _plt
from matplotlib.ticker import FuncFormatter
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Image, Spacer, Table, TableStyle, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from IPython.display import IFrame, FileLink, display
from datetime import datetime
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

import seaborn as sns
from docx import Document
from nltk.corpus import stopwords
from nltk.tokenize import sent_tokenize
import nltk
import spacy
from sklearn.metrics.pairwise import cosine_similarity
from sentence_transformers import SentenceTransformer, CrossEncoder
# J'ai ajouté CrossEncoder pour faire un reranking de précision (sans changer le modèle d'embeddings qui est all-MiniLM-L6-V2 et le plus performant en rapport qualité/ performance / vitesse / stabilité en CPU).
import gradio as gr

try:
    from openai import OpenAI as OpenAIClient
except ImportError:
    OpenAIClient = None
try:
    from mistralai import MistralClient
except ImportError:
    MistralClient = None
nltk.download("punkt")
nltk.download("stopwords")


# 1.bis Patch sauvegarde et affichage temporisé des graphiques + sauvegarde

# Dossier Content du Projet final
PROJECT_ROOT = os.path.abspath(".")
CONTENT_DIR = os.path.join(PROJECT_ROOT, "Content")
os.makedirs(CONTENT_DIR, exist_ok=True)

# Sauvegarde des fonctions matplotlib originales
_original_show = plt.show
_original_savefig = plt.savefig


_GRAPH_INDEX = 0
_FIG_ALREADY_SAVED = set()   
_PATCH_SAVED_TITLES = set()  # évite de resauver 2 fois le même titre via le patch (bug apparu sur version précédente et corrigé)

def _sanitize_filename(title: str) -> str:
    title = title.strip().replace("\n", " ")
    title = re.sub(r"[^\w\s-]", "", title)
    title = re.sub(r"\s+", "_", title)
    return title[:80]


# a) on patch savefig pour marquer le graphique comme "déjà sauvegardée"
def _patched_savefig(*args, **kwargs):
    fig = plt.gcf()
    _FIG_ALREADY_SAVED.add(id(fig))
    return _original_savefig(*args, **kwargs)

plt.savefig = _patched_savefig

# b) on patch show : affiche 3s, et ne sauvegarde que si le code n’a pas déjà fait savefig
def _patched_show(*args, **kwargs):
    global _GRAPH_INDEX

    fig = plt.gcf()
    ax = plt.gca()

    title = ax.get_title()
    if title:
        clean_title = _sanitize_filename(title)

        # Sauvegarde par patch uniquement si aucune sauvegarde n’a déjà eu lieu sur ce graphique
        if id(fig) not in _FIG_ALREADY_SAVED and clean_title not in _PATCH_SAVED_TITLES:
            _GRAPH_INDEX += 1
            filename = f"{_GRAPH_INDEX:02d}_{clean_title}.png"
            save_path = os.path.join(CONTENT_DIR, filename)

            try:
                fig.savefig(save_path, dpi=300, bbox_inches="tight")
                _FIG_ALREADY_SAVED.add(id(fig))
                _PATCH_SAVED_TITLES.add(clean_title)
                print(f"📊 Graphique sauvegardé : {filename}")
            except Exception as e:
                print(f"⚠️ Erreur sauvegarde graphique : {e}")

    # affichage 3s des graphiques (sans récursion)
    _original_show(block=False)
    fig.canvas.draw_idle()
    time.sleep(3)
    plt.close(fig)

plt.show = _patched_show


# 2. Chargement des données et des fichiers du projet
# Définit les chemins d accès au projet et charge le fichier Excel principal contenant le modèle financier et les hypothèses
PROJECT_PATH = "./"
CONTRATS_PATH = os.path.join(PROJECT_PATH, "Contrats")
MODEL_PATH = os.path.join(CONTRATS_PATH, "Model.xlsx")

assert os.path.isdir(CONTRATS_PATH), " Dossier Contrats introuvable"

print(f"Checking for Model.xlsx at: {MODEL_PATH}")
assert os.path.isfile(MODEL_PATH), " Fichier Model.xlsx introuvable"

excel_files = [f for f in os.listdir(CONTRATS_PATH) if f.lower().endswith(".xlsx")]
print(f"{len(excel_files)} fichiers Excel détectés dans le dossier Contrats :")
for file_name in excel_files:
    print(f"- {file_name}")

word_files = [f for f in os.listdir(CONTRATS_PATH) if f.lower().endswith(".docx")]
assert len(word_files) > 0, " Aucun fichier Word détecté"

print(f"{len(word_files)} fichiers Word détectés :")
for file_name in word_files:
    print(f"- {file_name}")

xls = pd.read_excel(
    MODEL_PATH,
    sheet_name=None,
    header=[0, 1],
    engine="openpyxl"
)

print("✅ Fichier Excel chargé en mémoire (xls disponible)")

required_sheets = ["Model", "Assumptions"]
for sheet in required_sheets:
    if sheet not in xls:
        raise ValueError(f" Feuille manquante dans l’Excel : {sheet}")

print("✅ Feuilles requises présentes :", required_sheets)


MODEL_SHEET = "Model"
ASSUMPTIONS_SHEET = "Assumptions"

MORTGAGE_COL = 'Mortgage Loan_Outstanding "end"'
SUB_COL = 'Subordinated Loan_Outstanding "end"'

OUTPUT_DIR = CONTENT_DIR
DEAL_PDF_PATH = os.path.join(CONTENT_DIR, "Deal_Summary.pdf")


def millions(x, pos):
    return f"{x / 1e6:,.0f} M"


def clean_multilevel_columns(df):
    df.columns = [
        "_".join(col).strip().replace("_level_0", "")
        for col in df.columns.values
    ]
    return df


# 3. Prétraitement des données financières + Graphiques financiers
# Cette section construit df_model à partir de la feuille "Model"
# et génère les 4 graphiques financiers, affichés à l'écran et sauvegardés dans OUTPUT_DIR.
# Ces 4 PNG sont ensuite intégrés dans la dernière page du PDF via make_deal_summary_pdf().

def _detect_date_column(cols):
    # colonne typique : 'Unnamed: 0_Beginning Date' après flatten multiindex
    for c in cols:
        if isinstance(c, str) and "beginning date" in c.lower():
            return c
    # fallback : première colonne
    return cols[0] if len(cols) else None


def summarize_trend(series: pd.Series) -> str:
    series = series.dropna()
    if series.empty:
        return "stable"
    if series.iloc[-1] > series.iloc[0]:
        return "increasing"
    if series.iloc[-1] < series.iloc[0]:
        return "decreasing"
    return "stable"


def generate_financial_charts(show_plots: bool = True) -> dict:
    """Génère les 4 graphiques financiers (PNG) + retourne un résumé (financial_summary)."""
    global df_model, df_assumptions, financial_summary

    # --- Construction df_model ---
    df_model = clean_multilevel_columns(xls[MODEL_SHEET].copy())

    date_col = _detect_date_column(df_model.columns)
    if date_col is None:
        raise RuntimeError("❌ Impossible de détecter la colonne Date dans la feuille Model.")

    df_model = df_model.rename(columns={date_col: "Date"})
    df_model["Date"] = pd.to_datetime(df_model["Date"], errors="coerce")
    df_model = df_model.dropna(subset=["Date"]).sort_values("Date")

    # Assumptions (utile pour PDF et autres extractions)
    df_assumptions = xls[ASSUMPTIONS_SHEET].dropna(how="all")

    # Indicateurs dérivés 
    df_model["delta_mortgage"] = df_model[MORTGAGE_COL].diff()
    df_model["delta_sub"] = df_model[SUB_COL].diff()

    df_model["total_outstanding"] = df_model[MORTGAGE_COL] + df_model[SUB_COL]
    df_model["subordination_ratio"] = df_model[SUB_COL] / df_model["total_outstanding"]

    financial_summary = {
        "mortgage_trend": summarize_trend(df_model[MORTGAGE_COL]),
        "sub_trend": summarize_trend(df_model[SUB_COL]),
        "avg_sub_ratio": float(df_model["subordination_ratio"].mean()),
    }

    # Graphique 1 : Capital structure share
    plt.figure(figsize=(12, 6))
    plt.stackplot(
        df_model["Date"],
        df_model[MORTGAGE_COL] / df_model["total_outstanding"],
        df_model[SUB_COL] / df_model["total_outstanding"],
        labels=["Mortgage Loan (%)", "Subordinated Loan (%)"],
        alpha=0.85,
    )
    plt.title("Capital Structure Composition Over Time")
    plt.xlabel("Date")
    plt.ylabel("Share of Total Debt")
    plt.legend()
    plt.ylim(0, 1)
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(f"{OUTPUT_DIR}/01_capital_structure_share.png", dpi=300)
    if show_plots:
        plt.show(block=True)
    plt.close()

    # Graphique 2 : Subordination ratio 
    plt.figure(figsize=(11, 5))
    plt.plot(
        df_model["Date"],
        df_model["subordination_ratio"],
        linewidth=2.5,
        label="Subordination Ratio (Protection Level)",
    )

    TARGET = 0.20
    plt.axhline(
        TARGET,
        linestyle="--",
        linewidth=1.8,
        label="Minimum Protection Threshold (20%)",
    )

    plt.fill_between(
        df_model["Date"],
        0,
        df_model["subordination_ratio"],
        alpha=0.15,
        label="Subordinated Loan Cushion (Mortgage Protection)",
    )

    if len(df_model) > 0:
        last_date = df_model["Date"].iloc[-1]
        last_value = df_model["subordination_ratio"].iloc[-1]
        plt.scatter(last_date, last_value, s=90, zorder=5)
        plt.annotate(
            "Mortgage fully exposed",
            (last_date, last_value),
            textcoords="offset points",
            xytext=(10, -15),
            fontsize=10,
            fontweight="bold",
        )

    plt.title("Subordination Ratio – Mortgage Risk Monitoring", fontweight="bold")
    plt.xlabel("Date")
    plt.ylabel("Subordination Ratio")
    ymax = df_model["subordination_ratio"].max()
    plt.ylim(0, min(1.1, float(ymax) * 1.15 if pd.notna(ymax) else 1.1))
    plt.legend(loc="upper left")
    plt.grid(alpha=0.25)
    plt.tight_layout()
    plt.savefig(f"{OUTPUT_DIR}/02_subordination_ratio.png", dpi=300)
    if show_plots:
        plt.show(block=True)
    plt.close()

    # Graphique 3 : Period-to-period debt change
    df_delta = df_model.dropna(subset=["delta_mortgage", "delta_sub"]).copy()

    plt.figure(figsize=(12, 5))
    plt.stem(
        df_delta["Date"],
        df_delta["delta_mortgage"],
        linefmt="C0-",
        markerfmt="C0o",
        basefmt="k-",
        label="Mortgage Δ",
    )
    plt.stem(
        df_delta["Date"],
        df_delta["delta_sub"],
        linefmt="C1-",
        markerfmt="C1o",
        basefmt="k-",
        label="Subordinated Δ",
    )

    plt.axhline(0, color="black", linewidth=0.8)
    plt.title("Period-to-Period Debt Change")
    plt.xlabel("Date")
    plt.ylabel("Change (Millions)")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.gca().yaxis.set_major_formatter(FuncFormatter(millions))
    plt.tight_layout()
    plt.savefig(f"{OUTPUT_DIR}/03_debt_variation.png", dpi=300)
    if show_plots:
        plt.show(block=True)
    plt.close()

    # Grzphique 4 : Snapshot
    if len(df_model) > 0:
        first = df_model.iloc[0]
        plt.figure(figsize=(6, 6))
        plt.pie(
            [first[MORTGAGE_COL], first[SUB_COL]],
            labels=["Mortgage Loan", "Subordinated Loan"],
            autopct="%1.1f%%",
            startangle=90,
        )
        plt.title("Debt Structure – Beginning of Period")
        plt.tight_layout()
        plt.savefig(f"{OUTPUT_DIR}/04_debt_snapshot.png", dpi=300)
        if show_plots:
            plt.show(block=True)
        plt.close()

    return financial_summary


# 4. Génération des graphiques financiers (avant Gradio)
financial_summary = generate_financial_charts(show_plots=True)

# 5. Génération automatique du PDF

def load_assumptions_for_deal(xlsx_path: str) -> pd.DataFrame:
    return pd.read_excel(xlsx_path, sheet_name="Assumptions", header=None)


def extract_deal_name_from_assumptions(df: pd.DataFrame) -> str:
    """A1 = 'Deal Name', B1 = nom du deal"""
    label = str(df.loc[0, 0]).strip()
    if "deal" in label.lower() and "name" in label.lower():
        return str(df.loc[0, 1]).strip()
    return str(df.loc[0, 1]).strip()


def extract_loans_from_assumptions(df: pd.DataFrame):
    """Extrait Mortgage Loan et Subordinated Loan depuis la feuille Assumptions."""
    instruments = ["Mortgage Loan", "Subordinated Loan"]
    loans = []
    for instr in instruments:
        row = df[df[0] == instr]
        if row.empty:
            continue
        r = row.iloc[0]
        loan = {
            "name": instr,
            "outstanding": r[1],
            "currency": r[2],
            "first_drawdown": r[3],
            "maturity": r[4],
            "frequency": r[5],
            "rate_type": r[6],
            "rate": r[7],
            "margin": r[8],
        }
        loans.append(loan)
    return loans


def extract_fees_from_assumptions(df: pd.DataFrame):
    """Extrait toutes les lignes dont le label contient 'Fee'."""
    fees_rows = df[df[0].astype(str).str.contains("Fee", case=False, na=False)]
    fees = []
    for _, r in fees_rows.iterrows():
        fee = {
            "label": str(r[0]).strip(),
            "amount": r[1],
            "currency": r[2],
            "start": r[3],
            "end": r[4],
            "frequency": r[5],
        }
        fees.append(fee)
    return fees


def get_assumption_value(df: pd.DataFrame, label: str) -> str:
    """
    Récupère la valeur associée à un label dans la feuille Assumptions.
    Colonne A = label, Colonne B = valeur.
    """
    for i in range(len(df)):
        cell_label = str(df.loc[i, 0]).strip().lower()
        if cell_label == label.strip().lower():
            return str(df.loc[i, 1]).strip()
    return "N/A"


def _format_money(value, currency=""):
    try:
        value = float(value)
        txt = f"{value:,.0f}".replace(",", " ")
    except Exception:
        txt = str(value)
    if currency:
        return f"{txt} {currency}"
    return txt

# 6. Génération du rapport PDF de synthèse du deal
# Construit un document PDF récapitulatif présentant la structure de dette, les frais, les questions clés et les graphiques de synthèse

def _safe_str(x) -> str:
    if x is None:
        return ""
    if isinstance(x, float) and pd.isna(x):
        return ""
    return str(x).strip()


def _format_date(value) -> str:
    """
    Formate une date au format AAAA/MM/DD
    (sans heures, minutes, secondes)
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    try:
        dt = pd.to_datetime(value)
        return dt.strftime("%Y/%m/%d")
    except Exception:
        return _safe_str(value)


def find_asset_image_path(contrats_path: str) -> str | None:
    """
    Cherche une image .jpg/.jpeg/.png dans Contrats.
    Priorité : fichiers dont le nom contient asset/photo/image/picture.
    """
    if not os.path.isdir(contrats_path):
        return None

    exts = (".jpg", ".jpeg", ".png")
    imgs = [f for f in os.listdir(contrats_path) if f.lower().endswith(exts)]
    if not imgs:
        return None

    priority_keywords = ("asset", "photo", "image", "picture")
    imgs_sorted = sorted(
        imgs,
        key=lambda n: (0 if any(k in n.lower() for k in priority_keywords) else 1, n.lower())
    )
    return os.path.join(contrats_path, imgs_sorted[0])


def scale_image_to_box(img_path: str, max_w_cm: float = 6.0, max_h_cm: float = 6.0) -> Image:
    """
    Retourne un flowable Image reportlab redimensionné au max_w/max_h (en cm),
    en conservant le ratio.
    """
    img_reader = ImageReader(img_path)
    iw, ih = img_reader.getSize()

    max_w = max_w_cm * cm
    max_h = max_h_cm * cm

    if iw <= 0 or ih <= 0:
        return Image(img_path, width=max_w, height=max_h)

    scale = min(max_w / iw, max_h / ih)
    return Image(img_path, width=iw * scale, height=ih * scale)


def extract_asset_info_from_assumptions(df: pd.DataFrame) -> dict:
    """
    Extrait des infos asset depuis la feuille Assumptions (header=None).
    Méthode robuste : on cherche des labels probables en colonne A (col 0)
    et on prend valeur col B (col 1) + currency col C (col 2) si pertinent.
    """
    # Normalise colonne 0 en string lower pour matcher des labels
    col0 = df[0].astype(str).str.strip().str.lower()

    def find_value(label_candidates, value_col=1, currency_col=2):
        idx = None
        for cand in label_candidates:
            m = col0 == cand
            if m.any():
                idx = int(df[m].index[0])
                break
        if idx is None:
            # fallback : "contains" (utile si les labels ne sont pas exactement égaux)
            for cand in label_candidates:
                m = col0.str.contains(cand, na=False)
                if m.any():
                    idx = int(df[m].index[0])
                    break
        if idx is None:
            return "", ""

        val = df.loc[idx, value_col] if value_col in df.columns else ""
        cur = df.loc[idx, currency_col] if currency_col in df.columns else ""
        return val, cur

    # Labels possibles (à élargir si besoin)
    asset_type_val, _ = find_value([
        "asset type", "type of asset", "asset", "asset description", "asset name"
    ])
    delivery_val, _ = find_value([
        "delivery", "delivery date", "delivery schedule", "delivery period"
    ])
    price_val, price_cur = find_value([
        "price", "purchase price", "asset price", "total price", "capex", "cost"
    ])
    vendor_val, _ = find_value([
        "vendor", "supplier", "seller", "manufacturer"
    ])
    serial_val, _ = find_value([
        "serial", "serial number", "msn", "hull number"
    ])

    # Formatage “intelligent”
    asset_info = {
        "Asset type": _safe_str(asset_type_val),
        "Delivery": _format_date(delivery_val),
        "Price": _format_money(price_val, _safe_str(price_cur)) if _safe_str(price_val) else "",
        "Vendor": _safe_str(vendor_val),
        "Serial / MSN": _safe_str(serial_val),
    }

    # Retire les champs vides (pour un rendu propre)
    asset_info = {k: v for k, v in asset_info.items() if _safe_str(v) != ""}
    return asset_info

# PDF – Formatage, Styles et Génération du Deal Summary

# Enregistrement des polices Arial 
# Gestion robuste des polices

FONT_DIR = os.path.dirname(__file__)

ARIAL_REGULAR = os.path.join(FONT_DIR, "Arial.ttf")
ARIAL_BOLD = os.path.join(FONT_DIR, "Arial Bold.ttf")

if os.path.exists(ARIAL_REGULAR) and os.path.exists(ARIAL_BOLD):
    pdfmetrics.registerFont(TTFont("Arial", ARIAL_REGULAR))
    pdfmetrics.registerFont(TTFont("Arial-Bold", ARIAL_BOLD))

    pdfmetrics.registerFontFamily(
        "Arial",
        normal="Arial",
        bold="Arial-Bold"
    )

    FONT = "Arial"
    FONT_BOLD = "Arial-Bold"
    print("✅ Police Arial chargée")

else:
    # Helvetica est native
    pdfmetrics.registerFontFamily(
        "Helvetica",
        normal="Helvetica",
        bold="Helvetica-Bold"
    )

    FONT = "Helvetica"
    FONT_BOLD = "Helvetica-Bold"
    print("⚠️ Arial non trouvée – Helvetica utilisée")


TITLE_STYLE = ParagraphStyle(
    "TITLE_STYLE",
    fontName=FONT_BOLD,
    fontSize=22,
    spaceAfter=20
)

# Formatage des dtaes

def _format_date(value) -> str:
    """
    Formate une date au format YYYY/MM/DD
    (sans heures, minutes, secondes)
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    try:
        dt = pd.to_datetime(value)
        return dt.strftime("%Y/%m/%d")
    except Exception:
        return str(value)


# Fonctions de formatage (montants / pourcentages)

def format_us_amount(value):
    """
    Formate un montant en format US :
    - pas de décimales
    - séparateurs de milliers
    Exemple : 180000000 -> 180 000 000
    """
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except Exception:
        return str(value)


def format_percent(value):
    """
    Formate un taux ou une marge en pourcentage.
    Exemple : 0.045 -> 4.50 %
    """
    try:
        return f"{float(value) * 100:.2f} %"
    except Exception:
        return str(value)


# Définition des styles PDF (Arial)

styles = getSampleStyleSheet()

TITLE_STYLE = ParagraphStyle(
    "TITLE_STYLE",
    fontName=FONT_BOLD,
    fontSize=22,
    spaceAfter=20
)

H1_16 = ParagraphStyle(
    "H1_16",
    fontName=FONT_BOLD,
    fontSize=16,
    spaceBefore=14,
    spaceAfter=12
)

H2_14 = ParagraphStyle(
    "H2_14",
    fontName=FONT_BOLD,
    fontSize=14,
    spaceBefore=12,
    spaceAfter=6
)

BOLD_14 = ParagraphStyle(
    "BOLD_14",
    fontName=FONT_BOLD,
    fontSize=14,
    spaceAfter=6
)

NORMAL_12 = ParagraphStyle(
    "NORMAL_12",
    fontName=FONT,
    fontSize=12,
    spaceAfter=5
)


# Questions à poser lors du kick-off meeting (PDF)

base_questions = [
    "Y a-t-il des IRS (Interest Rate Swaps) prévus ?",
    "Y a-t-il des Cross Currency Swaps associés ?",
]

additional_questions = [
    "Quel est le mode d'amortissement fiscal de l'actif ?",
    "Quelle est la durée d'amortissement fiscal de l'actif ?",
    "Existe-t-il des dispositions particulières de suramortissement fiscal "
    "liées au solaire ou au biométhane (suramortissement 40 % ou plafonné à 15 M€) ?",
    "Quel est le régime de TVA applicable aux fees ?",
]


def make_deal_summary_pdf(xlsx_path: str, output_pdf: str, charts_dir: str):
    """
    Génère le PDF Deal Summary en 3 pages :
    - Page 1 : Titre + Caractéristiques + Image
    - Page 2 : Données financières + Questions + TVA
    - Page 3 : 4 graphiques financiers
    """

    # Chargement des données depuis Assumptions

    df = load_assumptions_for_deal(xlsx_path)

    deal_name = extract_deal_name_from_assumptions(df)
    loans = extract_loans_from_assumptions(df)
    fees = extract_fees_from_assumptions(df)

    # Création du document PDF

    doc = SimpleDocTemplate(
        output_pdf,
        pagesize=A4,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm
    )

    story = []

    # Page 1 — Titre - Caractéristiques - Image
    story.append(Paragraph(deal_name, TITLE_STYLE))
    story.append(Paragraph("Caractéristiques", H1_16))

    characteristics = [
    ("Asset Type", get_assumption_value(df, "Asset type")),
    (
        "Asset Acquisition Date",
        _format_date(get_assumption_value(df, "Asset Acquisition Date"))
    ),
    (
        "Asset Delivery Date",
        _format_date(get_assumption_value(df, "Asset Delivery Date"))
    ),
    ("Duration", get_assumption_value(df, "Duration")),
    ("Asset Price", format_us_amount(get_assumption_value(df, "Asset Price"))),
    ("Currency", get_assumption_value(df, "Currency")),
]
    for label, value in characteristics:
        story.append(Paragraph(f"{label} : {value}", BOLD_14))

    story.append(Spacer(1, 20))

    img_path = find_asset_image_path(CONTRATS_PATH)
    if img_path:
        img = Image(img_path, width=16 * cm, height=9 * cm)
        img.hAlign = "CENTER"
        story.append(img)

    story.append(PageBreak())

    # Page 2 — Données Finacières
    story.append(Paragraph("Données Financières", H1_16))
    story.append(Spacer(1, 12))
    story.append(Spacer(1, 12))
    story.append(Spacer(1, 12))

    # Mortgage Loan
    story.append(Paragraph("Mortgage Loan", H2_14))
    for l in loans:
        if l["name"] == "Mortgage Loan":
            story.append(Paragraph(f"Outstanding : {format_us_amount(l['outstanding'])}", NORMAL_12))
            story.append(Paragraph(f"Rate : {format_percent(l['rate'])}", NORMAL_12))
            story.append(Paragraph(f"Margin : {format_percent(l['margin'])}", NORMAL_12))

    story.append(Spacer(1, 12))
    story.append(Spacer(1, 12))
    story.append(Spacer(1, 12))

    # Subordinated Loan
    story.append(Paragraph("Subordinated Loan", H2_14))
    for l in loans:
        if l["name"] == "Subordinated Loan":
            story.append(Paragraph(
                f"Outstanding : {format_us_amount(l['outstanding'])}",
                NORMAL_12
            ))
            story.append(Paragraph(
                f"Rate : {format_percent(l['rate'])}",
                NORMAL_12
            ))
            story.append(Paragraph(
                f"Margin : {format_percent(l['margin'])}",
                NORMAL_12
            ))

    story.append(Spacer(1, 12))
    story.append(Spacer(1, 12))
    story.append(Spacer(1, 12))

    # Fees
    story.append(Paragraph("Fees", H2_14))
    for f in fees:
        story.append(Paragraph(
            f"{f['label']} : "
            f"{format_us_amount(f['amount'])} {f['currency']} "
            f"(Periodicity : {f['frequency']})",
            NORMAL_12
        ))

    story.append(Spacer(1, 12))
    story.append(Spacer(1, 12))
    story.append(Spacer(1, 12))       # Page 2 — Données Finacières
    story.append(Paragraph("Données Financières", H1_16))

    # QuestionS kICK OFF (TVA)
    story.append(Spacer(1, 12))
    story.append(Paragraph("Questions à poser lors du kick off meeting", H2_14))

    for q in base_questions + additional_questions:
        story.append(Paragraph(f"• {q}", NORMAL_12))

        # Bloc TVA JUSTE APRÈS la question TVA
        if "régime de tva applicable aux fees" in q.lower():
            story.append(Spacer(1, 8))

            tva_data = [["Fee", "TVA Oui", "TVA Non"]]
            for f in fees:
                tva_data.append([f["label"], "", ""])

            tva_table = Table(
                tva_data,
                colWidths=[9 * cm, 3 * cm, 3 * cm]
            )
            tva_table.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("ALIGN", (1, 1), (-1, -1), "CENTER"),
            ]))

            story.append(tva_table)
            story.append(Spacer(1, 12))

    # Page 3 — Graphiques Financiers
   
    story.append(Paragraph("Graphiques Financiers", H1_16))

    chart_files = [
        "01_capital_structure_share.png",
        "02_subordination_ratio.png",
        "03_debt_variation.png",
        "04_debt_snapshot.png",
    ]

    charts = [
        Image(os.path.join(charts_dir, f), width=8 * cm, height=6 * cm)
        for f in chart_files
        if os.path.exists(os.path.join(charts_dir, f))
    ]

    if len(charts) == 4:
        grid = Table([[charts[0], charts[1]], [charts[2], charts[3]]])
        story.append(grid)

      # Génération finale du PDF
    doc.build(story)



print("📄 Lancement de la génération automatique du PDF...")
make_deal_summary_pdf(
    MODEL_PATH,
    DEAL_PDF_PATH,
    charts_dir=OUTPUT_DIR
)
print(f"✅ PDF généré avec succès : {DEAL_PDF_PATH}")

# 7. Paramètres du RAG — Volonté d'optimisation pour la précision

# Dans un premier temps j'ai relevé le seuil minimum de similarité de 0.40 à 0.55 pour (puis modification à 0.45 car le chatbot ne donne aucune réponse):
# - réduire les réponses basées sur des correspondances faibles
# - privilégier le "no answer" plutôt qu'une réponse approximative
MIN_SIMILARITY_LOW = 0.45
 
# J'ai ajouté un second seuil (0.75) pour (puis modification à 0.65 car le chatbot ne donne aucune réponse):
# - distinguer les réponses fiables des réponses incertaines
# - afficher un avertissement lorsque la réponse est plausible mais pas certaine
MIN_SIMILARITY_HIGH = 0.65

# J'ai conservé la variable MIN_SIMILARITY (compatibilité) en la reliant au seuil LOW.
MIN_SIMILARITY = MIN_SIMILARITY_LOW

# J'ai conservé TOP_K = 5 comme nombre de passages finaux montrés à l'utilisateur.
TOP_K = 5

# J'ai ajouté TOP_K_RETRIEVE = 30 pour (puis modification à 40 car le chatbot ne donne aucune réponse):
# - récupérer plus de candidats (rappel) avant reranking
# - laisser le cross-encoder trier finement pour la précision
TOP_K_RETRIEVE = 40

LLM_BACKEND = "mock"

# J'ai augmenté la taille des chunks de 120 à 200 pour (puis modification à 150 car le chatbot ne donne aucune réponse):
# - réduire la dilution sémantique des clauses longues
# - conserver des unités juridiques plus complètes (meilleure précision)
CHUNK_SIZE = 150

# J'ai augmenté l'overlap de 40 à 50 pour (puis modification à 60 car le chatbot ne donne aucune réponse):
# - limiter la perte d'informations à la frontière entre deux chunks
CHUNK_OVERLAP = 60

# Min chunk tokens inchangé : on évite d'indexer des fragments trop courts.
MIN_CHUNK_TOKENS = 30
embedding_model = SentenceTransformer(
    "sentence-transformers/all-MiniLM-L6-v2"
)

# J'ai ajouté un reranker cross-encoder pour améliorer la précision :
# - on récupère d'abord des candidats avec MiniLM (rapide)
# - puis on rerank les meilleurs passages avec un modèle "question+passage" (plus précis)
# - cela ne change PAS le modèle d'embeddings (contrainte respectée)
reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
STOPWORDS_LEGAL_EN = {
    "the", "of", "and", "or", "to", "a", "an", "for", "in", "on",
    "by", "with", "from", "as", "is", "are", "be", "this", "that", "such"
}

STOPWORDS_LEGAL_FR = {
    "le", "la", "les", "un", "une", "des", "du", "de", "d", "et", "ou",
    "dans", "en", "au", "aux", "par", "pour", "sur", "est", "sont", "sera",
    "seront", "ce", "cet", "cette", "ces"
}

STOPWORDS_LEGAL = STOPWORDS_LEGAL_EN | STOPWORDS_LEGAL_FR


def clean_text_baseline(text: str) -> str:
    """
    Prétraitement très simple (pipeline d'origine) :
    1) passage en minuscules
    2) normalisation des espaces
    3) filtrage léger de la ponctuation
    """
    if not isinstance(text, str):
        text = str(text)

    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w.,;:()-]", " ", text)
    return text.strip()


def normalize_dates_amounts(text: str) -> str:
    """
    Normalisation simplifiée des dates et montants :
    - Dates numériques : 01/02/2024, 1-2-24, 01.02.2024 -> __DATE__
    - Montants : 1 000 000 EUR, 1.000.000 €, 50,25 dollars -> __AMOUNT__
    (Heuristique)
    """
    if not isinstance(text, str):
        text = str(text)

    text = re.sub(r"\b\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}\b", "__DATE__", text)

    mois = (
        "january|february|march|april|may|june|july|august|september|october|november|december|"
        "janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|"
        "novembre|décembre|decembre"
    )
    text = re.sub(
        rf"\b\d{{1,2}}\s+(?:{mois})\s+\d{{4}}\b",
        "__DATE__",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\b\d[\d\s.,]*\s?(€|eur|euros|usd|dollars?)\b",
        "__AMOUNT__",
        text,
        flags=re.IGNORECASE,
    )
    return text


def tokenize_legal(text: str):
    """
    Tokenizer "spécialisé" pour le juridique (version simplifiée) :
    - garde les références d’articles ("article 5.2.1")
    - garde les nombres de type "5.2.1"
    - sinon, split mots / ponctuation
    """
    if not isinstance(text, str):
        text = str(text)

    pattern = r"""
        (article\s+\d+(?:\.\d+)*)
        |(section\s+\d+(?:\.\d+)*)
        |(\d+(?:\.\d+)+)
        |(\w+)
        |([^\w\s])
    """
    tokens = []
    for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.VERBOSE):
        tok = match.group(0)
        if tok.strip():
            tokens.append(tok)
    return tokens


def lemmatize_legal(tokens):
    """
    Lemmatisation juridique ultra simplifiée (placeholder).
    Un vrai système utiliserait spaCy / Stanza + modèle juridique.
    """
    lemma_map = {
        "shall": "shall",
        "may": "may",
        "including": "include",
        "includes": "include",
        "included": "include",
        "payments": "payment",
        "obligations": "obligation",
        "covenants": "covenant",
        "guarantees": "guarantee",
    }

    lemmas = []
    for t in tokens:
        t_low = t.lower()
        if t_low in lemma_map:
            lemmas.append(lemma_map[t_low])
        elif len(t_low) > 3 and t_low.endswith("s") and t_low[:-1].isalpha():
            lemmas.append(t_low[:-1])
        else:
            lemmas.append(t_low)
    return lemmas


def remove_stopwords(tokens):
    """
    Suppression de mots vides génériques et juridiques
    (“the”, “of”, “shall”, etc., y compris FR).
    """
    return [t for t in tokens if t.lower() not in STOPWORDS_LEGAL]


def detect_articles_crossrefs(text: str):
    """
    Détection (heuristique) d’articles / références croisées.
    Retourne une liste de matches (pour analyse / debug).
    """
    if not isinstance(text, str):
        text = str(text)

    patterns = [
        r"\barticle\s+\d+(?:\.\d+)*\b",
        r"\bsection\s+\d+(?:\.\d+)*\b",
        r"\bclause\s+\d+(?:\.\d+)*\b",
        r"\bart\.\s*\d+(?:\.\d+)*\b",
        r"\bsee\s+article\s+\d+(?:\.\d+)*\b",
    ]
    matches = []
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            matches.append(
                {
                    "match": m.group(0),
                    "start": m.start(),
                    "end": m.end(),
                    "pattern": pat,
                }
            )
    return matches


def clean_text_legal(text: str) -> str:
    """
    Pipeline de normalisation "juridique" enrichie :
    1) mise en minuscules
    2) normalisation des espaces
    3) normalisation des dates / montants
    4) tokenisation juridique spécialisée
    5) lemmatisation
    6) suppression de mots vides
    7) reconstruction d’un texte normalisé pour les embeddings
    """
    if not isinstance(text, str):
        text = str(text)

    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    text = normalize_dates_amounts(text)

    tokens = tokenize_legal(text)
    lemmas = lemmatize_legal(tokens)
    filtered = remove_stopwords(lemmas)

    return " ".join(filtered).strip()



def segment_clauses(text: str):
    """
    Découpe naïve par articles / paragraphes :
    - séparation sur retour ligne suivi d'un numéro ou du mot 'article'
    - on garde uniquement les segments d'une certaine longueur minimale
    """
    if not isinstance(text, str):
        text = str(text)

    return [
        c.strip()
        for c in re.split(r"\n(?=\d+\.|\barticle\b)", text)
        if len(c.strip()) > 50
    ]


def extract_text_from_docx(docx_path: str) -> str:
    document = Document(docx_path)
    full_text = []
    for para in document.paragraphs:
        full_text.append(para.text)
    return "\n".join(full_text)


def chunk_clause(clause_text: str, clause_id: int, filename: str):
    """
    Découpe une clause en sous-chunks qui se recouvrent légèrement.
    # J'ai changé cette note car le chunking est maintenant UTILISÉ dans le retrieval pour améliorer la précision.
    # (Avant : clauses entières => plus de bruit / dilution du score.)
    """
    tokens = tokenize_legal(clause_text.lower())
    chunks = []

    if not tokens:
        return chunks

    step = max(1, CHUNK_SIZE - CHUNK_OVERLAP)

    for i in range(0, len(tokens), step):
        window = tokens[i : i + CHUNK_SIZE]
        if len(window) < MIN_CHUNK_TOKENS:
            continue

        raw = " ".join(window)

        chunks.append(
            {
                "filename": filename,
                "clause_id": clause_id,
                "chunk_id": i,
                "raw": raw,
                "clean_baseline": clean_text_baseline(raw),
                "clean_legal": clean_text_legal(raw),
                "len_tokens": len(window),
            }
        )

    return chunks


contracts = []

for file in os.listdir(CONTRATS_PATH):
    if not file.lower().endswith(".docx"):
        continue

    full_path = os.path.join(CONTRATS_PATH, file)
    raw_text = extract_text_from_docx(full_path)
    clauses = segment_clauses(raw_text)

    contracts.append(
        {
            "filename": file,
            "raw_text": raw_text,
            "clauses": clauses,
            "nb_clauses": len(clauses),
        }
    )

df_contracts = pd.DataFrame(contracts)

rag_rows = []

# J'ai changé l'indexation "clause entière" vers une indexation en Chunks pour :
# - améliorer la précision (meilleur alignement question)
# - éviter qu'une clause longue dilue la similarité et fasse remonter de faux positifs
for _, row in df_contracts.iterrows():
    filename = row["filename"]
    for i, clause in enumerate(row["clauses"]):
        crossrefs = detect_articles_crossrefs(clause)

        # J'ai activé l'utilisation effective du chunking ici (avant : chunk_clause non utilisé)
        chunks = chunk_clause(clause, clause_id=i, filename=filename)

        # Si une clause ne produit pas de chunk (rare), on retombe sur un chunk "plein texte" par sécurité
        if not chunks:
            chunks = [{
                "filename": filename,
                "clause_id": i,
                "chunk_id": 0,
                "raw": clause,
                "clean_baseline": clean_text_baseline(clause),
                "clean_legal": clean_text_legal(clause),
                "len_tokens": len(tokenize_legal(clause.lower())),
            }]

        for ch in chunks:
            ch["crossrefs"] = crossrefs
            rag_rows.append(ch)

df_rag = pd.DataFrame(rag_rows)

# 8. Emfeddings — encodage en batch (plus rapide et plus stable)
# J'ai remplacé l'encodage ligne par ligne (.apply) par un encodage en batch pour :
# - réduire fortement le temps de calcul
# - garantir des embeddings cohérents (mêmes paramètres, même normalisation)
baseline_texts = df_rag["clean_baseline"].tolist()
legal_texts = df_rag["clean_legal"].tolist()

X_rag_baseline = np.asarray(
    embedding_model.encode(
        baseline_texts,
        batch_size=64,
        normalize_embeddings=True,
        show_progress_bar=True,
    ),
    dtype=np.float32,
)

X_rag_legal = np.asarray(
    embedding_model.encode(
        legal_texts,
        batch_size=64,
        normalize_embeddings=True,
        show_progress_bar=True,
    ),
    dtype=np.float32,
)

# Diagramme sur longueurs de texte avant / après nettoyage,

def plot_char_count_histograms(df_rag_local: pd.DataFrame):
    """
    Histogramme des longueurs de texte avant / après nettoyage,
    agrégé par document.
    """
    df_plot = df_rag_local.copy()
    df_plot["chars_raw"] = df_plot["raw"].str.len()
    df_plot["chars_baseline"] = df_plot["clean_baseline"].str.len()
    df_plot["chars_legal"] = df_plot["clean_legal"].str.len()

    df_doc = (
        df_plot.groupby("filename")[["chars_raw", "chars_baseline", "chars_legal"]]
        .sum()
        .reset_index()
    )

    df_long = df_doc.melt(
        id_vars="filename",
        value_vars=["chars_raw", "chars_baseline", "chars_legal"],
        var_name="text_version",
        value_name="num_characters",
    )

    df_long["text_version"] = df_long["text_version"].map(
        {
            "chars_raw": "Raw text",
            "chars_baseline": "Cleaned (baseline)",
            "chars_legal": "Cleaned (legal)",
        }
    )

    plt.figure(figsize=(10, 6))
    sns.barplot(
        data=df_long,
        x="filename",
        y="num_characters",
        hue="text_version",
    )

    plt.title("Nombre total de caractères par document\nAvant / après nettoyage")
    plt.ylabel("Nombre de caractères")
    plt.xlabel("Document")
    plt.xticks(rotation=30, ha="right")
    plt.grid(alpha=0.3)
    plt.legend(title="Version du texte")
    plt.tight_layout()
    plt.show()


plot_char_count_histograms(df_rag)

# 

def infer_contract_type(question: str):
    """
    Infère le type de contrat à partir de la question utilisateur.
    Utilisé pour filtrer l'espace de recherche avant similarité.
    """
    q = question.lower()

    if "mortgage" in q or "hypothec" in q:
        return "Mortgage"

    if "lease" in q or "tenant" in q or "rent" in q:
        return "lease"

    # J'ai corrigé avec l'aide de Chat GPT la faute de frappe "Subordianted" -> "Subordinated" pour :
    # - aligner le filtre avec le nom réel des fichiers ("Subordinated Loan...")
    # - éviter de rater tout le sous-ensemble de documents concernés
    if "subordinated" in q or "subordianted" in q or "mezzanine" in q:
        return "Subordinated"

    if (
        "shareholder" in q
        or "shareholders" in q
        or "equity" in q
        or "voting rights" in q
    ):
        return "Shareholders"

    return None


# 9. Pipeline RAG pour la recherche de clauses pertinentes
# Récupère les clauses les plus similaires aux questions en utilisant les embeddings et applique des règles de filtrage adaptées aux contrats
def retrieve_context(question: str, top_k: int = TOP_K, mode: str = "legal"):
    """
    mode = "baseline" : utilise clean_text_baseline + embeddings_baseline
    mode = "legal"    : utilise clean_text_legal + embeddings_legal

    - Filtrage du DataFrame RAG par type de contrat AVANT calcul de similarité

    Optimisation précision (ajoutée) :
    - J'ai augmenté le nombre de candidats récupérés (TOP_K_RETRIEVE) puis j'ai ajouté un RERANKING cross-encoder
      pour éliminer les faux positifs (précision ++).
    """

    contract_type = infer_contract_type(question)

    if contract_type:
        df_search = df_rag[
            df_rag["filename"].str.contains(contract_type, case=False, na=False)
        ]
        if df_search.empty:
            df_search = df_rag
    else:
        df_search = df_rag

    if mode == "baseline":
        q_clean = clean_text_baseline(question)
        X_search = X_rag_baseline[df_search.index]
    else:
        q_clean = clean_text_legal(question)
        X_search = X_rag_legal[df_search.index]

    q_emb = embedding_model.encode(
        q_clean,
        normalize_embeddings=True,
    ).reshape(1, -1)

    sims = cosine_similarity(q_emb, X_search)[0]
    if np.all(np.isnan(sims)):
        sims = np.zeros_like(sims)

    # J'ai changé le top_k initial (candidats) : 5 -> TOP_K_RETRIEVE (par défaut 30) pour :
    # - laisser plus de latitude au reranker (meilleure précision finale)
    cand_k = min(TOP_K_RETRIEVE, len(sims))
    cand_idx = sims.argsort()[::-1][:cand_k]

    candidates = df_search.iloc[cand_idx].copy()
    candidates["similarity"] = sims[cand_idx]
    candidates["mode"] = mode

    # Reranking
    # J'ai ajouté un reranking sur les candidats pour :
    # - évaluer finement les couples (question, passage)
    # - réduire les faux positifs même quand la similarité embedding est élevée
    pairs = [(question, r["raw"]) for _, r in candidates.iterrows()]
    raw_scores = reranker.predict(pairs)

    # J'ai appliqué une sigmoïde pour transformer les scores du cross-encoder en [0,1] :
    # - facilite l'interprétation + l'usage de seuils
    rerank_conf = 1.0 / (1.0 + np.exp(-np.asarray(raw_scores, dtype=np.float64)))
    candidates["rerank_score_raw"] = raw_scores
    candidates["rerank_confidence"] = rerank_conf

    candidates = candidates.sort_values("rerank_confidence", ascending=False)

    # Top passages finaux (affichés / envoyés au LLM)
    res = candidates.head(top_k).copy()

    confidence = float(res["rerank_confidence"].max()) if not res.empty else 0.0

    return res, confidence

def call_llm_rag(question: str, context_dicts, backend: str = LLM_BACKEND) -> str:
    """
    Placeholder : concatène simplement les clauses récupérées.
    Dans un vrai système, on appellerait un LLM distant.
    """
    context = "\n".join(
        f"[{c['filename']} – clause {c['clause_id']}]: {c['raw'][:200]}..."
        for c in context_dicts
    )
    return f"LLM({backend}) ANSWER\n\n{context}\n\nQ: {question}"

audit_log = []


def answer_with_rag(question: str, top_k: int = TOP_K, mode: str = "legal"):
    """
    Pipeline complet :
    - retrieve_context (désormais : retrieval + reranking)
    - application de deux seuils (LOW / HIGH) pour améliorer la précision
    - construction de la "réponse" (mock LLM)

    NOTE : J'ai corrigé un bug logique dans la condition "no answer" :
    - Avant : le no-answer ne se déclenchait presque jamais car ctx n'était jamais vide (top_k forcé)
    - Maintenant : on décide uniquement sur la confiance (score rerank calibré), ce qui est plus fiable.
    """

    ctx, confidence = retrieve_context(question, top_k=top_k, mode=mode)

    # 1) Refus total (précision maximale)
    if confidence < MIN_SIMILARITY_LOW:
        audit_log.append(
            {
                "question": question,
                "mode": mode,
                "confidence": confidence,
                "sources": ctx[["filename", "clause_id", "rerank_confidence"]].to_dict("records") if isinstance(ctx, pd.DataFrame) and not ctx.empty else [],
                "status": "NO_ANSWER_LOW_CONFIDENCE",
            }
        )
        return {
            "llm_answer": "Information non trouvée dans les documents fournis (confiance insuffisante).",
            "confidence": confidence,
            "retrieved_clauses": ctx if isinstance(ctx, pd.DataFrame) else pd.DataFrame(),
            "no_answer": True,
        }

    answer = call_llm_rag(
        question,
        ctx.to_dict("records"),
        backend=LLM_BACKEND,
    )

    # 2) Réponse avec avertissement (zone grise)
    if confidence < MIN_SIMILARITY_HIGH:
        # J'ai corrigé une chaîne non terminée (SyntaxError) en forçant une concaténation sur une seule ligne.
        answer = "Réponse basée sur des passages partiellement pertinents.\n\n" + answer
    audit_log.append(
        {
            "question": question,
            "mode": mode,
            "confidence": confidence,
            "sources": ctx[["filename", "clause_id", "rerank_confidence"]].to_dict("records"),
            "status": "ANSWERED",
        }
    )

    return {
        "llm_answer": answer,
        "confidence": confidence,
        "retrieved_clauses": ctx,
        "no_answer": False,
    }



def answer_with_rag_calibrated(
    question: str,
    top_k: int = TOP_K,
    mode: str = "legal",
) -> dict:
    """
    Wrapper standardisé pour le chatbot.
    - appelle answer_with_rag
    - renvoie toujours les mêmes clés :
        - llm_answer
        - retrieved_clauses (DataFrame, éventuellement vide)
        - global_calibrated_confidence (float)
        - no_answer (bool)
    Calibration OPTION A : identité (pas de recalibration complexe).
    """
    base = answer_with_rag(question, top_k=top_k, mode=mode)

    no_answer_flag = bool(base.get("no_answer", False))

    return {
        "llm_answer": base["llm_answer"],
        "retrieved_clauses": base.get("retrieved_clauses", pd.DataFrame()),
        "global_calibrated_confidence": float(base.get("confidence", 0.0)),
        "no_answer": no_answer_flag,
    }

evaluation_set = pd.DataFrame(
    [
        {
            "question": "What is the interest rate of the mortgage loan?",
            "target": "Mortgage_Loan.docx",
        },
        {
            "question": "What is the maturity of the mortgage loan?",
            "target": "Mortgage_Loan.docx",
        },
        {
            "question": "Is the mortgage loan amortising?",
            "target": "Mortgage_Loan.docx",
        },
        {
            "question": "What collateral secures the mortgage loan?",
            "target": "Mortgage_Loan.docx",
        },
        {
            "question": "What covenants apply to the mortgage loan?",
            "target": "Mortgage_Loan.docx",
        },
        {
            "question": "What is the interest rate of the subordinated loan?",
            "target": "Subordinated Loan.docx",
        },
        {
            "question": "What is the interest margin of the subordinated loan?",
            "target": "Subordinated Loan.docx",
        },
        {
            "question": "Is the subordinated loan secured?",
            "target": "Subordinated Loan.docx",
        },
        {
            "question": "What are the repayment terms of the subordinated loan?",
            "target": "Subordinated Loan.docx",
        },
        {
            "question": "Is early repayment allowed for the subordinated loan?",
            "target": "Subordinated Loan.docx",
        },
        {
            "question": "Which contract defines the lease?",
            "target": "Master finance lease..docx",
        },
        {
            "question": "What is the lease duration?",
            "target": "Master finance lease..docx",
        },
        {
            "question": "Who is the tenant?",
            "target": "Master finance lease..docx",
        },
        {
            "question": "Are break options defined in the lease?",
            "target": "Master finance lease..docx",
        },
        {
            "question": "How is rent indexed?",
            "target": "Master finance lease..docx",
        },
    
    ]
)


# 10. Calcul des métriques d'évaluation du pipeline RAG
# Mesure la qualité de la récupération de clauses et de la confiance associée en utilisant des jeux de questions annotées
def precision_at_k(
    eval_df: pd.DataFrame,
    k: int = 5,
    mode: str = "legal",
    use_threshold: bool = True,
) -> float:
    """
    Precision@k (optionnellement avec seuil MIN_SIMILARITY).
    """
    hits = []
    for _, r in eval_df.iterrows():
        ctx, conf = retrieve_context(r["question"], top_k=k, mode=mode)

        if use_threshold and conf < MIN_SIMILARITY:
            hits.append(False)
            continue

        hits.append(r["target"] in ctx["filename"].values)

    return float(np.mean(hits))


def mrr_at_k(eval_df: pd.DataFrame, k: int = 10, mode: str = "legal") -> float:
    """
    Mean Reciprocal Rank @k.
    """
    scores = []
    for _, r in eval_df.iterrows():
        res, _ = retrieve_context(r["question"], top_k=k, mode=mode)
        rank = 0.0
        for i, fname in enumerate(res["filename"], 1):
            if fname == r["target"]:
                rank = 1.0 / i
                break
        scores.append(rank)
    return float(np.mean(scores))


def avg_confidence(
    eval_df: pd.DataFrame,
    k: int = 5,
    mode: str = "legal",
) -> float:
    """
    Similarité moyenne top-1 (confiance brute).
    """
    confs = []
    for _, r in eval_df.iterrows():
        _, conf = retrieve_context(r["question"], top_k=k, mode=mode)
        confs.append(conf)
    return float(np.mean(confs))


metrics_before = {
    "Precision@5 (no threshold)": precision_at_k(
        evaluation_set, k=5, mode="baseline", use_threshold=False
    ),
    "Precision@5 (with threshold)": precision_at_k(
        evaluation_set, k=5, mode="baseline", use_threshold=True
    ),
    "MRR@10": mrr_at_k(evaluation_set, k=10, mode="baseline"),
    "Avg confidence": avg_confidence(evaluation_set, k=5, mode="baseline"),
}

metrics_after = {
    "Precision@5 (no threshold)": precision_at_k(
        evaluation_set, k=5, mode="legal", use_threshold=False
    ),
    "Precision@5 (with threshold)": precision_at_k(
        evaluation_set, k=5, mode="legal", use_threshold=True
    ),
    "MRR@10": mrr_at_k(evaluation_set, k=10, mode="legal"),
    "Avg confidence": avg_confidence(evaluation_set, k=5, mode="legal"),
}

print("=== METRICS BASELINE ===")
for k, v in metrics_before.items():
    print(f"{k}: {v:.3f}")

print("\n=== METRICS LEGAL PREPROCESSING ===")
for k, v in metrics_after.items():
    print(f"{k}: {v:.3f}")

df_metrics = []
for metric_name, val in metrics_before.items():
    df_metrics.append(
        {"metric": metric_name, "value": val, "pipeline": "baseline"}
    )
for metric_name, val in metrics_after.items():
    df_metrics.append(
        {"metric": metric_name, "value": val, "pipeline": "legal"}
    )

df_metrics = pd.DataFrame(df_metrics)

plt.figure(figsize=(10, 6))
sns.barplot(data=df_metrics, x="metric", y="value", hue="pipeline")
plt.title(
    "Comparaison des métriques RAG\nAvant / après prétraitement juridique + filtrage"
)
plt.ylabel("Score")
plt.ylim(0, 1)
plt.grid(alpha=0.3)
plt.xticks(rotation=20, ha="right")
plt.tight_layout()
plt.show()



def diag_short_clauses(
    eval_df: pd.DataFrame,
    mode: str = "legal",
    top_k: int = TOP_K,
) -> pd.DataFrame:
    """
    Pour chaque question d'évaluation :
    - récupère le top-1 (sur top_k)
    - enregistre la similarité et la longueur de la clause (en tokens)
    """
    rows = []
    for _, r in eval_df.iterrows():
        res, _ = retrieve_context(r["question"], top_k=top_k, mode=mode)
        top = res.iloc[0]
        rows.append(
            {
                "question": r["question"],
                "target": r["target"],
                "pipeline": mode,
                "top_filename": top["filename"],
                "top_clause_id": int(top["clause_id"]),
                "top_similarity": float(top["similarity"]),
                "top_clause_len_tokens": int(top["len_tokens"]),
            }
        )
    return pd.DataFrame(rows)


diag_base = diag_short_clauses(evaluation_set, mode="baseline", top_k=TOP_K)
diag_legal = diag_short_clauses(evaluation_set, mode="legal", top_k=TOP_K)

df_diag = pd.concat([diag_base, diag_legal], ignore_index=True)

SHORT_THRESHOLD = 15


def summarize_short_vs_long(df: pd.DataFrame, label: str):
    short = df[df["top_clause_len_tokens"] <= SHORT_THRESHOLD]
    long = df[df["top_clause_len_tokens"] > SHORT_THRESHOLD]

    print(f"\n--- Diagnostic {label} ---")
    print(f"Nombre de questions (short clauses): {len(short)}")
    print(f"Nombre de questions (long clauses) : {len(long)}")
    if len(short) > 0:
        print(
            f"Similarity moyenne (clauses courtes) : {short['top_similarity'].mean():.3f}"
        )
    if len(long) > 0:
        print(
            f"Similarity moyenne (clauses longues)  : {long['top_similarity'].mean():.3f}"
        )


summarize_short_vs_long(diag_base, "BASELINE")
summarize_short_vs_long(diag_legal, "LEGAL")

plt.figure(figsize=(8, 6))
sns.scatterplot(
    data=df_diag,
    x="top_clause_len_tokens",
    y="top_similarity",
    hue="pipeline",
)
plt.axvline(SHORT_THRESHOLD, linestyle="--", alpha=0.5, label="seuil clauses courtes")
plt.title(
    "MiniLM – Similarité top-1 vs longueur de clause\n"
    "(diagnostic clauses très courtes)"
)
plt.xlabel("Longueur de la clause (tokens)")
plt.ylabel("Similarité (cosine)")
plt.grid(alpha=0.3)
plt.legend()
plt.tight_layout()
plt.show()



def calibrate_confidence(raw_similarity):
    alpha = 12.0
    beta = 0.45
    return float(1 / (1 + np.exp(-alpha * (raw_similarity - beta))))


def calibrate_thresholds_from_audit(audit_log_list):
    """
    J'ai ajouté cette fonction pour calibrer automatiquement MIN_SIMILARITY_LOW et MIN_SIMILARITY_HIGH à partir
    de l'historique (audit_log) :

    - LOW  = quantile 25% des confiances sur les questions répondues (évite d'être trop permissif)
    - HIGH = quantile 60% (délimite une zone de réponse "fiable")

    Utilisation typique (après quelques dizaines/centaines de questions) :
        low, high = calibrate_thresholds_from_audit(audit_log)
    """
    if not audit_log_list:
        return MIN_SIMILARITY_LOW, MIN_SIMILARITY_HIGH

    df = pd.DataFrame(audit_log_list)
    if "confidence" not in df.columns or df.empty:
        return MIN_SIMILARITY_LOW, MIN_SIMILARITY_HIGH

    answered = df[df["status"] == "ANSWERED"]
    if answered.empty:
        return MIN_SIMILARITY_LOW, MIN_SIMILARITY_HIGH

    low = float(answered["confidence"].quantile(0.25))
    high = float(answered["confidence"].quantile(0.60))

    # garde-fous
    low = max(0.0, min(1.0, low))
    high = max(low, min(1.0, high))
    return low, high


def retrieve_context_calibrated(question, top_k=TOP_K, mode="legal"):
    ctx, _ = retrieve_context(question, top_k=top_k, mode=mode)

    ctx = ctx.copy()

    # J'ai changé la calibration : on utilise désormais le rerank_confidence (déjà dans [0,1]) car :
    # - il reflète mieux la pertinence réelle (cross-encoder question + passage)
    # - cela améliore la précision et rend les seuils plus stables
    ctx["similarity_raw"] = ctx["similarity"]
    ctx["confidence_calibrated"] = ctx["rerank_confidence"]

    global_confidence = float(ctx["confidence_calibrated"].max()) if not ctx.empty else 0.0

    return ctx, global_confidence


CONTRADICTION_PAIRS = [
    ("shall", "shall not"),
    ("must", "must not"),
    ("may", "may not"),
    ("included", "excluded"),
    ("allowed", "prohibited"),
]

def detect_contradiction_pair(text_a, text_b):
    ta, tb = text_a.lower(), text_b.lower()
    for pos, neg in CONTRADICTION_PAIRS:
        if (pos in ta and neg in tb) or (neg in ta and pos in tb):
            return True
    return False


def detect_inter_clause_contradictions(df_rag, X_rag, threshold=0.75):
    similarities = cosine_similarity(X_rag)
    contradictions = []

    for i in range(len(df_rag)):
        for j in range(i + 1, len(df_rag)):
            if similarities[i, j] < threshold:
                continue

            if detect_contradiction_pair(
                df_rag.iloc[i]["raw"],
                df_rag.iloc[j]["raw"]
            ):
                contradictions.append({
                    "file_1": df_rag.iloc[i]["filename"],
                    "clause_1": df_rag.iloc[i]["clause_id"],
                    "file_2": df_rag.iloc[j]["filename"],
                    "clause_2": df_rag.iloc[j]["clause_id"],
                    "similarity": similarities[i, j]
                })

    return pd.DataFrame(contradictions)


df_contradictions = detect_inter_clause_contradictions(df_rag, X_rag_legal)

print(f"✅ {len(df_contradictions)} contradictions potentielles détectées")



from sklearn.feature_extraction.text import TfidfVectorizer

tfidf_vectorizer = TfidfVectorizer(max_features=3000)
X_tfidf = tfidf_vectorizer.fit_transform(df_rag["clean_legal"])

def retrieve_tfidf(question, top_k=TOP_K):
    q = tfidf_vectorizer.transform([clean_text_legal(question)])
    sims = cosine_similarity(q, X_tfidf)[0]

    idx = sims.argsort()[::-1][:top_k]
    res = df_rag.iloc[idx].copy()
    res["similarity"] = sims[idx]

    return res


metrics_comparison = {
    "Embedding Precision@5": precision_at_k(evaluation_set, 5, mode="legal"),
    "TF-IDF Precision@5": np.mean([
        r.target in retrieve_tfidf(r.question, 5).filename.values
        for _, r in evaluation_set.iterrows()
    ])
}

plt.figure(figsize=(6, 4))
sns.barplot(
    x=list(metrics_comparison.keys()),
    y=list(metrics_comparison.values())
)
plt.title("TF-IDF vs Embeddings (Precision@5)")
plt.ylim(0, 1)
plt.grid(alpha=0.3)
plt.show()



def interpret_confidence(score: float) -> str:
    if score >= 0.75:
        return "🟢 Fiabilité élevée"
    if score >= 0.50:
        return "🟠 Fiabilité moyenne"
    return "🔴 Fiabilité faible"


def generate_deal_summary_for_ui():
    """Callback Gradio pour générer le Deal Summary et renvoyer le chemin du PDF."""
    # Assure que les 4 graphes financiers existent (utile si on exécute le script en mode partiel)
    for n in [
        "01_capital_structure_share.png",
        "02_subordination_ratio.png",
        "03_debt_variation.png",
        "04_debt_snapshot.png",
    ]:
        if not os.path.exists(os.path.join(OUTPUT_DIR, n)):
            generate_financial_charts(show_plots=False)
            break

    make_deal_summary_pdf(MODEL_PATH, DEAL_PDF_PATH, charts_dir=OUTPUT_DIR)
    return DEAL_PDF_PATH



def chat_callback(message, history, counter):
    if not message or not message.strip():
        return history, counter, "—", "Aucune source"

    counter += 1

    result = answer_with_rag_calibrated(
        message,
        top_k=5,
        mode="legal"
    )

    answer = result["llm_answer"]
    score = float(result["global_calibrated_confidence"])
    confidence_label = f"{score:.2f} — {interpret_confidence(score)}"

    ctx = result["retrieved_clauses"]

    if isinstance(ctx, pd.DataFrame) and not ctx.empty:
        sources = [
            f"- {row['filename']} | clause {row['clause_id']} (sim={row['similarity']:.3f})"
            for _, row in ctx.iterrows()
        ]
        sources_str = "\n".join(sources)
    else:
        sources_str = "Aucune source suffisamment pertinente."

    history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": answer},
    ]

    return history, counter, confidence_label, sources_str



# 11. Interface utilisateur pour la consultation et l analyse des contrats
# Crée une interface interactive permettant de poser des questions, visualiser les sources et générer le rapport de synthèse du deal

# Nom de l'opération  (source unique PDF + Chatbot)

_assumptions_df = load_assumptions_for_deal(MODEL_PATH)
DEAL_NAME = extract_deal_name_from_assumptions(_assumptions_df)

with gr.Blocks(
    title=f"French Tax Lease Assistant – Opération {DEAL_NAME}"
) as demo:

    gr.Markdown(
        f"# 🇫🇷 French Tax Lease Assistant\n## Opération : {DEAL_NAME}"
    )

    counter_state = gr.State(0)

    with gr.Row():

        with gr.Column(scale=3):
            chatbot = gr.Chatbot(
                elem_id="chatbot",
                label="Conversation",
                height=360
            )
            msg = gr.Textbox(
                placeholder="Posez votre question…",
                lines=1,
                show_label=False
            )
            send = gr.Button("Envoyer", size="sm")

        with gr.Column(scale=1):
            confidence_box = gr.Markdown("**Confiance**\n\n—")
            sources_box   = gr.Markdown("**Sources**\n\nAucune source")
            counter_box   = gr.Markdown("**Questions posées**\n\n0")
            pdf_button    = gr.Button("📄 Générer le Deal Summary")
            pdf_file      = gr.File(label="Deal_Summary.pdf généré")

    send.click(
        chat_callback,
        inputs=[msg, chatbot, counter_state],
        outputs=[chatbot, counter_state, confidence_box, sources_box]
    )

    msg.submit(
        chat_callback,
        inputs=[msg, chatbot, counter_state],
        outputs=[chatbot, counter_state, confidence_box, sources_box]
    )

    counter_state.change(
        lambda x: f"**Questions posées**\n\n{x}",
        inputs=counter_state,
        outputs=counter_box
    )

    pdf_button.click(
        generate_deal_summary_for_ui,
        outputs=pdf_file
    )


evaluation_data = [
    {
        "question": "What is the signature date of the Master Lease Agreement ?",
        "target_filename": "Master finance lease..docx",
        "target_clause_id": 0,
        "gold_answer": "The Master Finance Lease Agreement is dated 06/30/2025."
    },
    {
        "question": "Wh's the Lessor in the Master Lease Agreement ?",
        "target_filename": "Master finance lease..docx",
        "target_clause_id": 0,
        "gold_answer": "SNC PROJET is the Lessor."
    },
    {
        "question": "Which is the RCS number of CMA CGM S.A. ?",
        "target_filename": "Master finance lease..docx",
        "target_clause_id": 0,
        "gold_answer": "LThe RCS number of CMA CGM S.A. is 562 024 422."
    },
    {
        "question": "What is the global outstanding of the Mortgage Loan ?",
        "target_filename": "Model.xlsx",
        "target_clause_id": "N/A",
        "gold_answer": (
            f"The global outstanding of the Mortgage Loan is equal to 155 000 000 USD"
            f"{df_model[MORTGAGE_COL].iloc[-1]:,.2f} euros."
        )
    },
    {
        "question": "What is the trend of the subordinated loan outstanding according to the financial model ?",
        "target_filename": "Model.xlsx",
        "target_clause_id": "N/A",
        "gold_answer": (
            f"The trend of the subordinated loan outstanding is incresing "
            f"{financial_summary['sub_trend']}."
        )
    },
    {
        "question": "What is the maturity date of the mortgage loan mentioned in the assumptions ?",
        "target_filename": "Model.xlsx",
        "target_clause_id": "N/A",
        "gold_answer": "The maturity date of the mortgage loan is June 30, 2037."
    },
    {
        "question": "What type of interest rate is applied to the mortgage loan according to the assumptions ?",
        "target_filename": "Model.xlsx",
        "target_clause_id": "N/A",
        "gold_answer": "A variable interest rate is applied to the mortgage loan."
    }

]

df_evaluation = pd.DataFrame(evaluation_data)
print("Dataset d'\u00e9valuation cr\u00e9\u00e9:")
print(df_evaluation.head())

evaluation_results = []

for _, row in df_evaluation.iterrows():
    question = row["question"]

    ctx_df, confidence = retrieve_context_calibrated(question, top_k=TOP_K)

    top_hit = ctx_df.iloc[0]

    evaluation_results.append({
        "question": question,
        "target_filename": row["target_filename"],
        "retrieved_filename": top_hit["filename"],
        "target_clause_id": row["target_clause_id"],
        "retrieved_clause_id": top_hit["clause_id"],
        "similarity_raw": top_hit["similarity_raw"],
        "confidence_calibrated": top_hit["confidence_calibrated"],
        "document_match": top_hit["filename"] == row["target_filename"]
    })

results = []

for index, row in df_evaluation.iterrows():
    question = row["question"]
    gold_answer = row["gold_answer"]
    target_filename = row["target_filename"]
    target_clause_id = row["target_clause_id"]

    try:
        rag_output = answer_with_rag_calibrated(question)
        llm_answer = rag_output["llm_answer"]
        confidence = rag_output["global_calibrated_confidence"]
        retrieved_clauses = rag_output["retrieved_clauses"]

        retrieved_correct_source = False
        if target_filename != "N/A":
            for _, clause_row in retrieved_clauses.iterrows():
                if clause_row["filename"] == target_filename:
                    retrieved_correct_source = True
                    break

        answer_match = False
        if isinstance(gold_answer, str) and isinstance(llm_answer, str):
            gold_keywords = set(gold_answer.lower().split())
            llm_keywords = set(llm_answer.lower().split())
            common_keywords = gold_keywords.intersection(llm_keywords)
            if len(common_keywords) / len(gold_keywords) > 0.5:
                answer_match = True

        results.append({
            "question": question,
            "gold_answer": gold_answer,
            "llm_answer": llm_answer,
            "confidence": confidence,
            "retrieved_correct_source": retrieved_correct_source,
            "answer_match": answer_match
        })

    except Exception as e:
        print(f"Error processing question '{question}': {e}")
        results.append({
            "question": question,
            "gold_answer": gold_answer,
            "llm_answer": f"Error: {e}",
            "confidence": 0.0,
            "retrieved_correct_source": False,
            "answer_match": False
        })

df_results = pd.DataFrame(results)

print("\n--- Résultats d'évaluation détaillés ---")
for index, row in df_results.iterrows():
    print(f"Question: {row['question']}")
    print(f"  LLM Answer: {row['llm_answer'][:100]}...")
    print(f"  Confidence: {row['confidence']:.2f}")
    print(f"  Retrieved Correct Source: {row['retrieved_correct_source']}")
    print(f"  Answer Match (simple): {row['answer_match']}")
    print("-----------------------------------------")


retrieval_accuracy = df_results["retrieved_correct_source"].mean()
print(f"\nPrécision de la récupération des sources correctes: {retrieval_accuracy:.2%}")

answer_accuracy = df_results["answer_match"].mean()
print(f"Précision de la correspondance des réponses (simple): {answer_accuracy:.2%}")

avg_confidence = df_results["confidence"].mean()
print(f"Score de confiance moyen: {avg_confidence:.2f}")

metrics_df = pd.DataFrame({
    'Metric': ['Retrieval Accuracy', 'Answer Accuracy (Simple)', 'Average Confidence'],
    'Value': [retrieval_accuracy, answer_accuracy, avg_confidence]
})

plt.figure(figsize=(10, 6))
sns.barplot(x='Metric', y='Value', data=metrics_df)
plt.title("Performance Globale du Chatbot RAG")
plt.ylabel("Score")
plt.ylim(0, 1)
plt.show()

plt.figure(figsize=(10, 6))
sns.histplot(df_results['confidence'], bins=10, kde=True)
plt.title("Distribution des scores de confiance du Chatbot")
plt.xlabel("Score de Confiance Calibré")
plt.ylabel("Nombre de questions")
plt.show()

# 12. Renommage Final des graphiques et lancement du Chatbot

def renumber_pngs_in_content():
    """
    Renumérote les fichiers PNG du dossier Content sans collision (Windows safe).
    Procède en deux étapes :
    1) renommage temporaire
    2) renommage final 01_, 02_, ...
    """

    pngs = sorted(
        f for f in os.listdir(CONTENT_DIR)
        if f.lower().endswith(".png")
    )

    # --- Étape 1 : renommage temporaire ---
    temp_paths = []
    for i, fname in enumerate(pngs):
        src = os.path.join(CONTENT_DIR, fname)
        tmp = os.path.join(CONTENT_DIR, f"__tmp__{i}_{fname}")
        os.rename(src, tmp)
        temp_paths.append(tmp)

    # --- Étape 2 : renommage définitif ---
    for i, tmp_path in enumerate(temp_paths, start=1):
        original = os.path.basename(tmp_path).replace("__tmp__", "")
        # on conserve le nom après le premier underscore
        new_name = f"{i:02d}_" + "_".join(original.split("_")[1:])
        dst = os.path.join(CONTENT_DIR, new_name)
        os.rename(tmp_path, dst)

    print("✅ Renumérotation des PNG terminée sans collision")


# À exécuter après génération de tous les graphiques
renumber_pngs_in_content()

# Lancement du Chatbot

if __name__ == "__main__":
    print("✅ Tous les calculs, graphiques et évaluations sont terminés.")
    print("🚀 Lancement de l'interface chatbot Gradio...")

    demo.launch(debug=True, inbrowser=True, prevent_thread_lock=True)
print("✅ Script terminé proprement.")


