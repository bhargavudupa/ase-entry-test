"""Check requirements.txt package and pinned-version availability on PyPI."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version


PYPI_JSON_URL = "https://pypi.org/pypi/{name}/json"
REQUEST_TIMEOUT = 10


def canonicalize_name(name: str) -> str:
	"""Normalize a package name for consistent PyPI lookup and caching."""
	return re.sub(r"[-_.]+", "-", name).lower()


def read_requirements(path: Path) -> list[Requirement]:
	"""Read and parse requirement entries from a requirements file."""
	requirements: list[Requirement] = []
	pending = ""

	for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
		line = line.strip()
		if not line or line.startswith("#"):
			continue

		# Only whitespace-prefixed hashes start comments; URL fragments remain intact.
		pending += re.sub(r"\s+#.*$", "", line).strip()
		# Join backslash-continued lines before parsing the requirement.
		if pending.endswith("\\"):
			pending = pending[:-1].rstrip() + " "
			continue

		try:
			requirements.append(Requirement(pending))
		except InvalidRequirement as error:
			raise ValueError(f"line {line_number}: {error}") from error
		pending = ""

	if pending:
		try:
			requirements.append(Requirement(pending))
		except InvalidRequirement as error:
			raise ValueError(f"line {line_number}: {error}") from error

	return requirements


def fetch_project(name: str) -> dict | None:
	"""Fetch PyPI metadata for a package, returning None when it is not found."""
	url = PYPI_JSON_URL.format(name=quote(name, safe=""))
	request = Request(url, headers={"User-Agent": "requirements-pypi-checker/1.0"})
	try:
		with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
			return json.load(response)
	except HTTPError as error:
		# A 404 means the project is absent; other HTTP errors are inconclusive.
		if error.code == 404:
			return None
		raise RuntimeError(f"PyPI returned HTTP {error.code}") from error
	except (URLError, TimeoutError, json.JSONDecodeError) as error:
		raise RuntimeError(str(error)) from error


def pinned_versions(requirement: Requirement) -> list[tuple[str, str]]:
	"""Return the exact version pins from a requirement, excluding wildcards."""
	pins = []
	for specifier in requirement.specifier:
		# Wildcard pins describe a family of versions, not one release to look up.
		if specifier.operator not in ("==", "===") or specifier.version.endswith(".*"):
			continue
		pins.append((specifier.operator, specifier.version))
	return pins


def release_exists(releases: dict, operator: str, requested_version: str) -> str | None:
	"""Find the PyPI release matching an exact version pin, if one exists."""
	if operator == "===":
		return next((version for version in releases if version == requested_version), None)

	# PEP 440-equivalent spellings should match the same published release.
	try:
		requested = Version(requested_version)
	except InvalidVersion:
		return None

	for version in releases:
		try:
			if Version(version) == requested:
				return version
		except InvalidVersion:
			continue
	return None


def release_date(files: list[dict]) -> datetime | None:
	"""Return the earliest upload timestamp for a PyPI release's files."""
	dates = []
	for file_info in files:
		timestamp = file_info.get("upload_time_iso_8601") or file_info.get("upload_time")
		if not timestamp:
			continue
		try:
			dates.append(datetime.fromisoformat(timestamp.replace("Z", "+00:00")))
		except ValueError:
			continue
	# A release may have several files; its release date is the earliest upload.
	return min(dates) if dates else None


def main() -> int:
	"""Check requirements against PyPI, print a summary, and return a status code."""
	# Accept an optional file path; otherwise use requirements.txt beside this script.
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"requirements",
		nargs="?",
		type=Path,
		default=Path(__file__).with_name("requirements.txt"),
		help="requirements file to check (default: requirements.txt beside this script)",
	)
	args = parser.parse_args()

	try:
		requirements = read_requirements(args.requirements)
	except (OSError, ValueError) as error:
		# Stop before making network requests if the input file cannot be parsed.
		print(f"Could not read requirements: {error}", file=sys.stderr)
		return 2

	# Keep result categories separate so lookup failures are not reported as missing packages.
	projects: dict[str, dict] = {}
	packages_not_on_pypi = []
	versions_not_on_pypi = []
	check_errors = []
	pinned_releases = {}

	for requirement in requirements:
		project_name = requirement.name
		key = canonicalize_name(project_name)
		# Reuse one PyPI lookup when the project appears in multiple requirements.
		if key not in projects:
			try:
				projects[key] = {"name": project_name, "data": fetch_project(project_name)}
			except RuntimeError as error:
				# A network/API error leaves this dependency unchecked, not confirmed missing.
				check_errors.append(f"{project_name}: {error}")
				continue

		project = projects[key]
		if project["data"] is None:
			# Without project metadata, there is no release list to check pinned versions against.
			if project_name not in packages_not_on_pypi:
				packages_not_on_pypi.append(project_name)
			continue

		releases = project["data"].get("releases", {})
		for operator, version in pinned_versions(requirement):
			matched_version = release_exists(releases, operator, version)
			label = f"{project_name}{operator}{version}"
			if matched_version is None:
				if label not in versions_not_on_pypi:
					versions_not_on_pypi.append(label)
				continue

			release_key = (key, matched_version)
			if release_key not in pinned_releases:
				pinned_releases[release_key] = {
					"label": f"{project_name}={matched_version}",
					"date": release_date(releases.get(matched_version, [])),
				}

	# Print the requested summary counts after every dependency has been checked.
	print(f"Dependencies checked: {len(requirements)}")
	print(f"Packages not on PyPI: {len(packages_not_on_pypi)}")
	print(f"Versions not on PyPI: {len(versions_not_on_pypi)}")

	# Compare dates only for pinned releases that PyPI confirms and dates exist for.
	dated_pins = [pin for pin in pinned_releases.values() if pin["date"] is not None]
	if dated_pins:
		oldest = min(dated_pins, key=lambda pin: pin["date"])
		print(f"Oldest pinned release: {oldest['label']} ({oldest['date'].date().isoformat()})")
	elif pinned_releases:
		print("Oldest pinned release: Release dates unavailable from PyPI")
	else:
		print("Oldest pinned release: None")

	if check_errors:
		for error in check_errors:
			print(f"PyPI check error: {error}", file=sys.stderr)

	# Return failure if any package/version is invalid or any PyPI check could not complete.
	return 1 if packages_not_on_pypi or versions_not_on_pypi or check_errors else 0


if __name__ == "__main__":
	raise SystemExit(main())
