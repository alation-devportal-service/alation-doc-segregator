# Alation Docs Segregator & Exporter 🚀

A robust, Streamlit-based utility designed to parse, segregate, and translate complex Sphinx reStructuredText (reST) documentation repositories. 

This tool was built to facilitate the migration and dual-maintenance of Alation's documentation by cleanly separating **Alation Cloud Service** and **CustomerManaged** (On-Prem) content. Furthermore, it acts as a migration evaluation tool by offering automated translation from Sphinx reST to modern Markdown formats like Mintlify (MDX) and GitBook (MD).

## ✨ Key Features

* **Intelligent Segregation:** Scans `.rst` files for specific Sphinx `.. include::` product labels and separates them into distinct Cloud and On-Prem directory structures.
* **Recursive Dependency Tracing:** Uses a Breadth-First Search (BFS) algorithm to trace `.. include::`, `:doc:`, `:ref:`, and `.. image::` directives. If a shared snippet or image is required by a tagged document, it is automatically pulled into the segregated environment.
* **Interactive Orphan Review:** Generates a CSV report and an interactive UI table of "Untagged" files, allowing technical writers to manually assign environments to orphaned files before the final build.
* **Multi-Format Export Engine:** * **Sphinx reST:** Preserves the exact original repository structure and syntax.
    * **Mintlify (MDX):** Converts includes to `<Snippet>` components and admonitions to custom tags.
    * **GitBook (MD):** Converts includes to `{% include %}` templating and admonitions to blockquotes.
    * **Flat Markdown:** Flattens the documentation by recursively translating and inlining shared text fragments directly into the host pages.
* **Fail-Safe Inlining:** If the translation engine detects a missing dependency (e.g., an include file that wasn't successfully copied), it automatically reads the original file from the source repo and inlines its content to guarantee zero data loss.

---

## 🛠 Prerequisites

* **Python 3.9+**
* **Git** installed on your system.
* A **GitHub Personal Access Token (PAT)** with `repo` scope to clone your private documentation repository.

---

## 📦 Installation & Local Setup

1. **Clone this repository:**

   ```bash
   git clone [https://github.com/your-username/alation-docs-segregator.git](https://github.com/your-username/alation-docs-segregator.git)
   cd alation-docs-segregator
   ```
2. **Create a virtual environment (Recommended):**

   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows use: venv\Scripts\activate
   ```
3. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```
4. **Configure Streamlit Secrets:**
   Create a `.streamlit/secrets.toml` file in the root of your project to securely store your target repository URL.

   ```ini,toml
   # .streamlit/secrets.toml
   REPO_URL = "[github.com/Alation/alation_docs.git](https://github.com/Alation/alation_docs.git)"

**Note:** Do not include `https://` in the `REPO_URL`. The app injects your `PAT` into the URL dynamically during runtime.

## 🚀 Usage Guide

1. **Start the Application:**

   ```bash
   streamlit run app.py
   ```
2. **Authenticate:** Enter your GitHub PAT in the sidebar.

3. **Fetch Repository:** Click **Clone / Pull Latest Docs Repository** to download the latest `.rst` files to a temporary local workspace.

4. **Analyze Dependencies:** Click **Scan for Tags & Dependencies**. The app will map the entire Sphinx project, identifying `Cloud` files, `CustomerManaged` files, and `Untagged` files.

5. **Review Untagged Files:** Use the interactive data table to manually assign missing files to an environment. You can also download this list as a CSV.

6. **Generate Output:** Select your desired output format (reST, Mintlify, GitBook, or Flat MD) and click **Apply Manual Tags & Generate ZIP**.

## 🧠 How the Segregation Logic Works

The app categorizes files based on specific strings present at the top of the `.rst` pages:

  - **Cloud:** Triggered by `Cloud_Label.rst`. Goes to the **Alation Cloud Service** folder.

  - **CustomerManaged:** Triggered by `CustomerManaged_Label.rst`. Goes to the **CustomerManaged** folder.

  - **Both:** Triggered by `CloudAndCustomerManaged_Label.rst`. The file (and its dependencies) are duplicated into both output folders.
    

Essential build files (like `conf.py` and `Makefile`) are automatically preserved and copied to all environments to ensure output directories remain fully buildable in Sphinx.

## 📜 License

This project is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).

Under this license, you are free to use, modify, and distribute this software. However, if you modify this application and provide access to it over a network (e.g., deploying it as a web app on Streamlit Cloud for others to use), you must also make the complete underlying source code of your modified version available to the users interacting with it.

See the LICENSE file for the full text.
