"""
File and document handler for the outreach agent.
Manages the PDF presentation and email template files.
"""

import logging
import os
from pathlib import Path

from modules.private_files import make_private_dir

logger = logging.getLogger(__name__)


class FileHandler:
    """
    Handles file system operations for the outreach agent.
    Manages PDF presentation, email templates, and data directories.
    """

    def __init__(self, project_root: str | None = None):
        if project_root:
            self.root = Path(project_root)
        else:
            self.root = Path(__file__).resolve().parent.parent
        # The email texts are the client's words, like its profile, so a
        # different client's texts can be pointed to with CLIENT_TEMPLATES_DIR.
        # An explicit project root always keeps its own templates/ folder.
        override = os.environ.get("CLIENT_TEMPLATES_DIR")
        self.templates = (
            Path(override) if override and not project_root else self.root / "templates"
        )

    # ─────────────────────────────────────────────────────────────────
    # PDF Operations
    # ─────────────────────────────────────────────────────────────────

    def get_pdf_path(self, pdf_path: str) -> str | None:
        """
        Resolve and validate the PDF presentation path.
        Returns absolute path if file exists, None otherwise.
        """
        path = Path(pdf_path)
        if not path.is_absolute():
            path = self.root / path

        if path.exists() and path.is_file():
            logger.info(f"PDF found: {path}")
            return str(path)

        logger.warning(f"PDF not found at: {path}")
        return None

    def pdf_problem(self, pdf_path: str) -> tuple[str, str | None]:
        """The PDF's absolute path, and why it cannot be attached, or None.

        Missing, a folder, empty or unreadable: each would send a first email
        that says "please find attached" with nothing attached. Quiet, unlike
        get_pdf_path, because it is asked at every first email.
        """
        path = Path(pdf_path or "")
        if not path.is_absolute():
            path = self.root / path
        if not path.exists():
            return str(path), "is missing"
        if not path.is_file():
            return str(path), "is a folder, not a file"
        try:
            with open(path, "rb") as f:
                if not f.read(1):
                    return str(path), "is empty"
        except OSError as e:
            return str(path), f"cannot be read ({e.strerror or e})"
        return str(path), None

    def get_pdf_size(self, pdf_path: str) -> int | None:
        """Get the file size of the PDF in bytes."""
        resolved = self.get_pdf_path(pdf_path)
        if resolved:
            return Path(resolved).stat().st_size
        return None

    # ─────────────────────────────────────────────────────────────────
    # Template Operations
    # ─────────────────────────────────────────────────────────────────

    def get_email_template(
        self, template_name: str = "email_template.html"
    ) -> str | None:
        """
        Load the HTML email template.
        Looks in the templates/ directory.
        """
        template_path = self.templates / template_name

        if template_path.exists():
            return template_path.read_text(encoding="utf-8")

        logger.warning(f"Email template not found: {template_path}")
        return None

    def get_template_variants(self) -> list:
        """Return A/B body-template variants as ``[(name, html), ...]``.

        Matches ``templates/email_template*.html`` — the main template plus any
        ``email_template_b.html``, ``email_template_c.html``, ... variants.
        Follow-up templates (``followup_*.html``) are intentionally excluded.
        """
        templates_dir = self.templates
        variants = []
        if not templates_dir.exists():
            return variants
        for f in sorted(templates_dir.glob("email_template*.html")):
            # email_template_it.html is the Italian first email, not an arm
            # of the English test: taken for one, it would go to every
            # second foreign customer.
            if f.stem.endswith("_it"):
                continue
            try:
                variants.append((f.stem, f.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError) as e:
                # Unreadable file, or a variant that is not UTF-8. Skipping it
                # drops one A/B arm, so it must not be silent.
                logger.warning(f"Could not read template variant {f.name}: {e}")
        return variants

    def template_stamps(self) -> dict:
        """What identifies the saved version of each first-email template.

        Keyed like get_template_variants, so a variant added or removed is a
        change too; None for a file that cannot be checked.
        """
        stamps = {}
        for f in self.templates.glob("email_template*.html"):
            try:
                st = f.stat()
                stamps[f.stem] = (st.st_mtime_ns, st.st_size)
            except OSError:
                stamps[f.stem] = None
        return stamps

    # ─────────────────────────────────────────────────────────────────
    # Directory Management
    # ─────────────────────────────────────────────────────────────────

    def ensure_directories(self):
        """Create required directories if they don't exist."""
        make_private_dir(self.root / "data")
        dirs = [
            self.root / "assets",
            self.root / "templates",
            self.root / "static" / "css",
            self.root / "static" / "js",
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Ensured directory: {d}")

    # ─────────────────────────────────────────────────────────────────
    # Validation
    # ─────────────────────────────────────────────────────────────────

    def validate_setup(self, pdf_path: str) -> dict:
        """
        Validate that all required files and directories exist.
        Returns a status dict with pass/fail for each requirement.
        """
        results = {
            "data_dir": (self.root / "data").exists(),
            "assets_dir": (self.root / "assets").exists(),
            "templates_dir": self.templates.exists(),
            "email_template": (self.templates / "email_template.html").exists(),
            "pdf_presentation": self.get_pdf_path(pdf_path) is not None,
            "env_file": (self.root / ".env").exists(),
        }
        results["all_ok"] = all(results.values())
        return results
