# Description: Provides documentation helper configuration or automation for MultiProtocolGateway.
# File: generate_indexes.py
#
# Copyright 2026 Kevin Burke
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://apache.org
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import urllib.parse
from pathlib import Path


def extract_first_header(file_path: Path) -> str | None:
    """Extract the first header from a markdown file."""
    try:
        with file_path.open(encoding="utf-8") as file:
            for line in file:
                clean_line: str = line.strip()
                if clean_line.startswith("#"):
                    return clean_line.lstrip("#").strip()
    except (OSError, UnicodeDecodeError):
        pass
    return None

def generate_readme(directory: str | Path, folder_order: list[str] | None = None, output_file: str = "README.md") -> None:
    base_path: Path = Path(directory).resolve()
    target_readme: Path = base_path / output_file
    folder_order = folder_order or []

    # Define supported image extensions
    image_exts: set[str] = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

    content: list[str] = ["# README Index\n", "This README file contains an index of all files.\n", "## File List\n"]

    note_file: Path = base_path / "note.md"
    if note_file.exists():
        content.extend(["\n## Additional Notes\n", note_file.read_text(encoding="utf-8")])

    folder_lines: dict[str, list[str]] = {}

    for path in base_path.rglob("*"):
        # Skip hidden files/folders and the README itself
        if path.name == output_file or any(part.startswith(".") for part in path.parts):
            continue

        if path.is_dir():
            if path != base_path:
                generate_readme(path)
            continue

        rel_path: Path = path.relative_to(base_path)
        folder_key: str = str(rel_path.parent).replace("\\", "/")

        if folder_key not in folder_lines:
            folder_lines[folder_key] = [f"**{folder_key}**\n"]

        file_url: str = urllib.parse.quote(rel_path.as_posix())
        line_item: str = f"- [{path.name}]({file_url})"

        # Check file type
        suffix: str = path.suffix.lower()
        if suffix == ".md":
            header: str | None = extract_first_header(path)
            if header:
                line_item += f" - {header}"
        elif suffix in image_exts:
            #  Add a small camera emoji to identify images
            line_item: str = f"- 🖼️ [{path.name}]({file_url})"

        folder_lines[folder_key].append(line_item)

    with target_readme.open("w", encoding="utf-8") as f:
        f.write("\n".join(content) + "\n")
        all_folders: list[str] = list(folder_lines.keys())
        remaining: list[str] = [folder for folder in all_folders if folder not in folder_order]

        for folder in remaining + folder_order:
            if folder in folder_lines:
                f.write("\n".join(folder_lines[folder]) + "\n\n")

if __name__ == "__main__":
    script_dir: Path = Path(__file__).parent
    directory_to_index: Path = script_dir / ".."
    generate_readme(directory_to_index, ["3rdparty", "3rdparty/protocols"])
