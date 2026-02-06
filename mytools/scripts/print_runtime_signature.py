from __future__ import annotations

import json

import frequency_db_utils


def main() -> None:
	print(json.dumps(frequency_db_utils.runtime_signature(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
	main()
