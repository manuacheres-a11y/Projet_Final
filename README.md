# French Tax Lease Assistant

## Description

**French Tax Lease Assistant** est un système d’analyse financière et juridique dédié aux opérations de **French Tax Lease**.  
Il s’appuie sur une architecture **Retrieval-Augmented Generation (RAG)** afin de permettre l’extraction, l’analyse et l’interrogation fiable de documents financiers et contractuels complexes.

Le projet vise à fournir un outil explicable, traçable et sécurisé pour l’analyse de deals de leasing fiscal, notamment dans des environnements à forte contrainte réglementaire.

---

## Objectifs du projet

- Centraliser l’analyse financière et juridique d’un deal de leasing  
- Automatiser l’extraction et la structuration des données contractuelles  
- Générer des livrables financiers et juridiques exploitables  
- Fournir un assistant conversationnel basé exclusivement sur les documents fournis  

---

## Fonctionnalités principales

- 📊 Chargement et validation d’un **modèle financier Excel**
- 📄 Analyse automatique de **contrats juridiques Word (.docx)**
- 📈 Génération de **graphiques financiers**
- 📑 Création d’un **rapport PDF de synthèse du deal**
- 💬 Assistant conversationnel juridique basé sur un **moteur RAG**

---

## Prérequis techniques

- **Python 3.11 uniquement**  
  ⚠️ Le projet n’est pas compatible avec les autres versions de Python.

---

## Structure du projet

```
.
├── Projet_final_Vdef.py
├── requirements.txt
├── Contrats/
│   ├── Model.xlsx
│   └── *.docx
└── Content/
```

---

## Données d’entrée attendues

Les fichiers doivent être placés dans le dossier **Contrats/** :

- `Model.xlsx` : modèle financier  
- Un ou plusieurs contrats juridiques au format **Word (.docx)**

---

## Installation

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Exécution

```bash
python Projet_final_Vdef.py
```

---

## Architecture et logique interne

Le projet repose sur :
1. Traitement des données financières  
2. Analyse des contrats juridiques  
3. Génération de livrables  
4. Moteur RAG  
5. Interface utilisateur  

---

## Moteur RAG

Le moteur RAG repose sur :
- Prétraitement linguistique
- Segmentation en chunks
- Embeddings sémantiques
- Recherche par similarité
- Reranking par cross-encoder
- Seuils de confiance

Les réponses sont **strictement basées sur les documents fournis**.

---

## Livrables générés

- Graphiques financiers  
- Rapport PDF de synthèse  
- Interface interactive d’interrogation  

---

## Bibliothèques principales

pandas, numpy, openpyxl, matplotlib, seaborn, reportlab, python-docx, nltk, sentence-transformers, transformers, torch, scikit-learn, gradio

---

## Avertissement

Ce projet ne constitue pas un avis juridique ou fiscal.

---

## Licence

À définir.
