"""frequency_db_utils package."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from . import word_adder
from .word_adder import add_word

# Bump this whenever you want to verify the app is using the latest code.
BUILD_ID = "2025-12-29-ipa-wrapper-normalization"


def runtime_signature() -> dict:
	"""Return a small, runtime-checkable signature for debugging deployments.

	Use this from any consuming app to verify which copy/version of this package
	is actually being imported.
	"""
	return {
		"package": __name__,
		"version": __version__,
		"build_id": BUILD_ID,
		"package_file": __file__,
		"word_adder_file": getattr(word_adder, "__file__", None),
	}


def get_version() -> str:
	"""Return installed package version (best-effort)."""
	try:
		return version("frequency-db-utils")
	except PackageNotFoundError:
		# Editable/local usage
		return "0.0.0+local"


__version__ = get_version()

__all__ = ["add_word", "word_adder", "get_version", "__version__", "BUILD_ID", "runtime_signature"]
