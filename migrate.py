#!/usr/bin/env python3
"""
Obsidian to Notion Migration Script
====================================
Migrates Obsidian markdown files to Notion, preserving directory hierarchy
and embedded file references.

The script recursively processes any directory structure:
- Directories become Notion pages
- Markdown files become child pages with content
- Embedded files (images, PDFs, etc.) are uploaded to the note that references them
- Special directories (files/, .obsidian/, .git/) are automatically skipped

Usage:
    python migrate.py /path/to/vault "https://www.notion.so/teamspace/Page-abc123"
    python migrate.py /path/to/vault/Projects "https://www.notion.so/teamspace/Page-abc123"

Requirements:
    pip install notion-client requests

Setup:
    1. Create a Notion integration at https://www.notion.so/my-integrations
    2. Share your target Notion page/teamspace with the integration
    3. Set NOTION_TOKEN environment variable (or use --token flag)

Author: Ryan Cabeen, ryan@saturnatech.com
"""

import argparse
import csv
import os
import re
import sys
import logging
import time
import mimetypes
import requests
from pathlib import Path
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field
from urllib.parse import urlparse, quote, unquote

from notion_client import Client as NotionClient
from notion_client.errors import APIResponseError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# =============================================================================
# CLI Argument Parsing
# =============================================================================

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Migrate Obsidian vault to Notion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Migrate entire vault
    python migrate.py ~/Documents/MyVault "https://www.notion.so/myteam/Page-abc123"

    # Migrate a specific subfolder
    python migrate.py ~/Documents/MyVault/Projects "https://www.notion.so/myteam/Projects-abc123"

    # With token flag
    python migrate.py ~/Documents/MyVault "https://www.notion.so/myteam/Page-abc123" --token secret_xxx

    # Dry run to preview
    python migrate.py ~/Documents/MyVault "https://www.notion.so/myteam/Page-abc123" --dry-run
        """
    )
    
    parser.add_argument(
        "source",
        type=str,
        help="Path to Obsidian vault directory"
    )
    
    parser.add_argument(
        "destination",
        type=str,
        help="Notion page URL (e.g., https://www.notion.so/teamspace/Page-Title-abc123def456)"
    )
    
    parser.add_argument(
        "--token",
        type=str,
        default=os.environ.get("NOTION_TOKEN", ""),
        help="Notion integration token (default: NOTION_TOKEN env var)"
    )
    
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview migration without making changes"
    )
    
    parser.add_argument(
        "--skip-files",
        action="store_true",
        help="Skip file uploads (migrate notes only)"
    )
    
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )
    
    return parser.parse_args()


def extract_page_id(notion_url: str) -> str:
    """Extract page ID from a Notion URL."""
    # Handle various Notion URL formats:
    # https://www.notion.so/workspace/Page-Title-abc123def456...
    # https://notion.so/abc123def456...
    # https://www.notion.so/abc123def456...
    # Just the ID: abc123def456...
    
    # Remove query params and fragments
    url = notion_url.split('?')[0].split('#')[0]
    
    # Extract the last path segment
    if '/' in url:
        last_segment = url.rstrip('/').split('/')[-1]
    else:
        last_segment = url
    
    # The ID is the last 32 hex characters (with or without dashes)
    uuid_pattern = r'([a-f0-9]{8}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{12})'
    match = re.search(uuid_pattern, last_segment, re.IGNORECASE)
    
    if match:
        page_id = match.group(1).replace('-', '')
        return f"{page_id[:8]}-{page_id[8:12]}-{page_id[12:16]}-{page_id[16:20]}-{page_id[20:]}"
    
    # Try last 32 hex chars
    hex_only = re.sub(r'[^a-f0-9]', '', last_segment.lower())
    if len(hex_only) >= 32:
        page_id = hex_only[-32:]
        return f"{page_id[:8]}-{page_id[8:12]}-{page_id[12:16]}-{page_id[16:20]}-{page_id[20:]}"
    
    raise ValueError(f"Could not extract page ID from: {notion_url}")


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class Config:
    """Migration configuration."""
    vault_path: Path
    notion_token: str
    parent_page_id: str
    dry_run: bool = False
    skip_files: bool = False
    verbose: bool = False


# =============================================================================
# Notion File Uploader (Direct Upload via Public API)
# =============================================================================

class NotionFileUploader:
    """Handles uploading files directly to Notion using the file upload API."""

    IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp', '.ico', '.tiff'}
    VIDEO_EXTENSIONS = {'.mp4', '.mov', '.webm', '.avi', '.mkv'}
    AUDIO_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.m4a', '.flac'}
    PDF_EXTENSIONS = {'.pdf'}

    def __init__(self, notion_token: str, config: Config):
        self.notion_token = notion_token
        self.config = config
        self.upload_cache = {}
        self.failed_uploads = []  # Track failed uploads for reporting
        self.successful_uploads = []  # Track successful uploads for reporting
        self.api_base = "https://api.notion.com/v1"
        self.headers = {
            "Authorization": f"Bearer {notion_token}",
            "Notion-Version": "2022-06-28",
        }

    def upload_file(self, file_path: Path, parent_page_id: str) -> Optional[dict]:
        """
        Upload a file to Notion using the file upload API.
        Returns dict with upload info or None on failure.
        """
        if self.config.skip_files:
            return None

        if not file_path.exists():
            logger.warning(f"File not found: {file_path}")
            return None

        cache_key = str(file_path.resolve())
        if cache_key in self.upload_cache:
            return self.upload_cache[cache_key]

        if self.config.dry_run:
            logger.info(f"[DRY RUN] Would upload: {file_path.name}")
            result = {
                "file_upload_id": f"dry-run-{file_path.name}",
                "type": self._get_block_type(file_path),
                "name": file_path.name,
            }
            self.upload_cache[cache_key] = result
            return result

        try:
            # Step 1: Create file upload object
            mime_type, _ = mimetypes.guess_type(str(file_path))
            if not mime_type:
                mime_type = 'application/octet-stream'

            create_payload = {
                "filename": file_path.name,
                "content_type": mime_type
            }

            create_response = requests.post(
                f"{self.api_base}/file_uploads",
                headers={**self.headers, "Content-Type": "application/json"},
                json=create_payload
            )

            if create_response.status_code != 200:
                reason = f"API create failed ({create_response.status_code}): {create_response.text[:100]}"
                logger.warning(f"File upload create failed ({create_response.status_code}): {create_response.text[:200]}")
                return self._handle_fallback(file_path, reason)

            create_data = create_response.json()
            file_upload_id = create_data.get("id")

            if not file_upload_id:
                logger.warning(f"No file upload ID returned for {file_path.name}")
                return self._handle_fallback(file_path, "No file upload ID returned")

            # Step 2: Send file content using multipart/form-data
            with open(file_path, 'rb') as f:
                files = {'file': (file_path.name, f, mime_type)}
                send_response = requests.post(
                    f"{self.api_base}/file_uploads/{file_upload_id}/send",
                    headers={
                        "Authorization": f"Bearer {self.notion_token}",
                        "Notion-Version": "2022-06-28",
                    },
                    files=files
                )

            if send_response.status_code != 200:
                reason = f"API send failed ({send_response.status_code}): {send_response.text[:100]}"
                logger.warning(f"File upload send failed ({send_response.status_code}): {send_response.text[:200]}")
                return self._handle_fallback(file_path, reason)

            result = {
                "file_upload_id": file_upload_id,
                "type": self._get_block_type(file_path),
                "name": file_path.name,
            }
            self.upload_cache[cache_key] = result
            self.successful_uploads.append({
                "file": str(file_path),
                "name": file_path.name,
                "file_upload_id": file_upload_id,
                "parent_page_id": parent_page_id,
            })
            logger.info(f"Uploaded: {file_path.name}")
            return result

        except Exception as e:
            logger.error(f"Failed to upload {file_path}: {e}")
            return self._handle_fallback(file_path, f"Exception: {e}")

    def _handle_fallback(self, file_path: Path, reason: str = "Unknown") -> dict:
        """Handle cases where direct upload isn't available."""
        logger.info(f"Using placeholder for: {file_path.name} (manual upload may be needed)")
        self.failed_uploads.append({
            "file": str(file_path),
            "name": file_path.name,
            "reason": reason
        })
        return {
            "file_upload_id": None,
            "type": self._get_block_type(file_path),
            "name": file_path.name,
            "local_path": str(file_path),
            "needs_manual_upload": True
        }

    def _get_block_type(self, file_path: Path) -> str:
        """Determine the Notion block type for a file."""
        suffix = file_path.suffix.lower()

        if suffix in self.IMAGE_EXTENSIONS:
            return "image"
        elif suffix in self.VIDEO_EXTENSIONS:
            return "video"
        elif suffix in self.AUDIO_EXTENSIONS:
            return "audio"
        elif suffix in self.PDF_EXTENSIONS:
            return "pdf"
        else:
            return "file"


# =============================================================================
# Markdown Parser (Obsidian-flavored)
# =============================================================================

@dataclass
class ParsedNote:
    """Represents a parsed Obsidian note."""
    title: str
    date: Optional[datetime] = None
    content: str = ""
    frontmatter: dict = field(default_factory=dict)
    file_references: list = field(default_factory=list)
    internal_links: list = field(default_factory=list)


class ObsidianParser:
    """Parses Obsidian markdown files."""

    FRONTMATTER_PATTERN = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)
    WIKILINK_PATTERN = re.compile(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]')
    EMBED_PATTERN = re.compile(r'!\[\[([^\]]+)\]\]')
    MD_IMAGE_PATTERN = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
    # Markdown links to local files: [text](path) - not starting with ! and path not a URL
    MD_FILE_LINK_PATTERN = re.compile(r'(?<!!)\[([^\]]+)\]\(([^)]+)\)')

    # File extensions that indicate a local file link (not a web page)
    FILE_EXTENSIONS = {
        '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp',
        '.mp4', '.mov', '.webm', '.avi', '.mkv',
        '.mp3', '.wav', '.ogg', '.m4a', '.flac',
        '.zip', '.tar', '.gz', '.rar', '.7z',
        '.txt', '.csv', '.json', '.xml', '.yaml', '.yml',
        '.html', '.htm', '.ipynb',
    }
    
    def __init__(self, vault_path: Path):
        self.vault_path = vault_path
        self.unresolved_references = []  # Track unresolved file references
    
    def parse_file(self, file_path: Path) -> ParsedNote:
        """Parse an Obsidian markdown file."""
        content = file_path.read_text(encoding='utf-8')
        
        title = file_path.stem
        date = self._extract_date_from_filename(title)
        if date:
            title = re.sub(r'^\d{4}-\d{2}-\d{2}\s*', '', title).strip()
            if not title:
                title = date.strftime("%Y-%m-%d")
        
        frontmatter = {}
        frontmatter_match = self.FRONTMATTER_PATTERN.match(content)
        if frontmatter_match:
            frontmatter = self._parse_frontmatter(frontmatter_match.group(1))
            content = content[frontmatter_match.end():]
        
        file_refs = self._find_file_references(content, file_path.parent, file_path)
        internal_links = self._find_internal_links(content)
        
        return ParsedNote(
            title=title,
            date=date,
            content=content,
            frontmatter=frontmatter,
            file_references=file_refs,
            internal_links=internal_links
        )
    
    def _extract_date_from_filename(self, filename: str) -> Optional[datetime]:
        match = re.match(r'^(\d{4}-\d{2}-\d{2})', filename)
        if match:
            try:
                return datetime.strptime(match.group(1), "%Y-%m-%d")
            except ValueError:
                pass
        return None
    
    def _parse_frontmatter(self, frontmatter_text: str) -> dict:
        result = {}
        for line in frontmatter_text.strip().split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                result[key.strip()] = value.strip()
        return result
    
    def _find_file_references(self, content: str, note_dir: Path, note_path: Path) -> list:
        refs = []
        files_dir = note_dir / "files"

        # Track positions we've already matched to avoid duplicates
        matched_positions = set()

        # Obsidian embeds: ![[filename]]
        for match in self.EMBED_PATTERN.finditer(content):
            ref = match.group(1)
            file_path = self._resolve_file_path(ref, note_dir, files_dir, note_path)
            if file_path:
                refs.append((match.group(0), file_path, match.start(), match.end()))
                matched_positions.add((match.start(), match.end()))

        # Markdown images: ![alt](path)
        for match in self.MD_IMAGE_PATTERN.finditer(content):
            path = match.group(2)
            if not path.startswith(('http://', 'https://', 'data:')):
                file_path = self._resolve_file_path(path, note_dir, files_dir, note_path)
                if file_path:
                    refs.append((match.group(0), file_path, match.start(), match.end()))
                    matched_positions.add((match.start(), match.end()))

        # Markdown file links: [text](path) - for local files only
        for match in self.MD_FILE_LINK_PATTERN.finditer(content):
            # Skip if this position was already matched (e.g., as an image)
            if (match.start(), match.end()) in matched_positions:
                continue

            path = match.group(2)
            # Skip web URLs
            if path.startswith(('http://', 'https://', 'data:', '#', 'mailto:')):
                continue

            # Check if it looks like a file (has a known extension)
            path_lower = path.lower()
            has_file_ext = any(path_lower.endswith(ext) for ext in self.FILE_EXTENSIONS)

            if has_file_ext:
                file_path = self._resolve_file_path(path, note_dir, files_dir, note_path)
                if file_path:
                    refs.append((match.group(0), file_path, match.start(), match.end()))

        return refs
    
    def _resolve_file_path(self, ref: str, note_dir: Path, files_dir: Path, note_path: Path = None) -> Optional[Path]:
        ref = ref.strip()

        # URL-decode the reference (e.g., %20 -> space)
        ref_decoded = unquote(ref)

        # Try both encoded and decoded versions
        for r in [ref_decoded, ref] if ref_decoded != ref else [ref]:
            if files_dir.exists():
                candidate = files_dir / r
                if candidate.exists():
                    return candidate
                candidate = files_dir / Path(r).name
                if candidate.exists():
                    return candidate

            candidate = note_dir / r
            if candidate.exists():
                return candidate

            candidate = self.vault_path / r
            if candidate.exists():
                return candidate

            if files_dir.exists():
                ref_name = Path(r).name
                for f in files_dir.iterdir():
                    if f.name == ref_name or f.stem == Path(r).stem:
                        return f

        # Fallback: search entire vault for file with same basename
        basename = Path(unquote(ref)).name
        found = self._search_vault_for_file(basename)
        if found:
            logger.info(f"Found '{basename}' at alternate location: {found}")
            return found

        logger.warning(f"Could not resolve file reference: {ref}")
        self.unresolved_references.append({
            "note": str(note_path) if note_path else "Unknown",
            "reference": ref
        })
        return None

    def _search_vault_for_file(self, basename: str) -> Optional[Path]:
        """Search the entire vault for a file with the given basename."""
        # Also try URL-decoded version
        basename_decoded = unquote(basename)

        for root, dirs, files in os.walk(self.vault_path):
            # Skip hidden directories and common non-content dirs
            dirs[:] = [d for d in dirs if not d.startswith('.')]

            for filename in files:
                if filename == basename or filename == basename_decoded:
                    return Path(root) / filename

        return None
    
    def _find_internal_links(self, content: str) -> list:
        links = []
        for match in self.WIKILINK_PATTERN.finditer(content):
            start = match.start()
            if start > 0 and content[start-1] == '!':
                continue
            links.append(match.group(1))
        return links


# =============================================================================
# Notion Block Builder
# =============================================================================

class NotionBlockBuilder:
    """Converts markdown content to Notion blocks."""

    # Pattern for bare URLs (not inside markdown link syntax)
    BARE_URL_PATTERN = re.compile(r'(?<!\()(https?://[^\s<>\[\]()]+)')

    def __init__(self, file_uploader: NotionFileUploader):
        self.file_uploader = file_uploader
        self.pending_files = []  # Files that need upload after page creation
        self.title_cache = {}  # Cache for fetched page titles

    def _is_valid_url(self, url: str) -> bool:
        """Check if a URL is valid for Notion."""
        try:
            parsed = urlparse(url)
            # Must have scheme and netloc (domain)
            if not parsed.scheme or not parsed.netloc:
                return False
            # Scheme must be http or https
            if parsed.scheme not in ('http', 'https'):
                return False
            # Domain must have at least one dot (basic check)
            if '.' not in parsed.netloc:
                return False
            return True
        except Exception:
            return False

    def _sanitize_url(self, url: str) -> Optional[str]:
        """Sanitize and validate a URL for Notion. Returns None if invalid."""
        if not url:
            return None

        # Strip whitespace and common surrounding characters
        url = url.strip().strip('<>').strip('"\'')

        # Handle URLs that might have been mangled
        if not url.startswith(('http://', 'https://')):
            return None

        # Validate
        if not self._is_valid_url(url):
            logger.debug(f"Invalid URL skipped: {url}")
            return None

        return url

    def _fetch_page_title(self, url: str) -> Optional[str]:
        """Fetch the page title from a URL. Returns None on failure."""
        if url in self.title_cache:
            return self.title_cache[url]

        try:
            response = requests.get(url, timeout=5, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; ObsidianMigrator/1.0)'
            })
            if response.status_code == 200:
                # Look for <title> tag
                match = re.search(r'<title[^>]*>([^<]+)</title>', response.text, re.IGNORECASE)
                if match:
                    title = match.group(1).strip()
                    # Clean up common title suffixes
                    title = re.sub(r'\s*[|\-–—]\s*[^|\-–—]+$', '', title).strip()
                    if title:
                        self.title_cache[url] = title
                        return title
        except Exception:
            pass  # Silently fail, we'll just use the URL

        self.title_cache[url] = None
        return None
    
    def build_blocks(self, parsed_note: ParsedNote, note_dir: Path, page_id: str = None) -> list:
        """Convert parsed note to Notion blocks."""
        blocks = []
        content = parsed_note.content
        
        # Upload files and build replacement info
        file_info_map = {}
        for original_ref, file_path, start, end in parsed_note.file_references:
            if page_id:
                file_info = self.file_uploader.upload_file(file_path, page_id)
            else:
                file_info = {"name": file_path.name, "type": self.file_uploader._get_block_type(file_path), "local_path": str(file_path)}
            file_info_map[original_ref] = (file_path, file_info)
        
        lines = content.split('\n')
        i = 0
        in_code_block = False
        code_block_lang = ""
        code_block_lines = []
        
        while i < len(lines):
            line = lines[i]
            
            # Handle code blocks
            if line.startswith('```'):
                if not in_code_block:
                    in_code_block = True
                    code_block_lang = line[3:].strip() or "plain text"
                    code_block_lines = []
                else:
                    blocks.append(self._code_block('\n'.join(code_block_lines), code_block_lang))
                    in_code_block = False
                i += 1
                continue
            
            if in_code_block:
                code_block_lines.append(line)
                i += 1
                continue
            
            # Check for file embeds
            embed_handled = False
            for original_ref, (file_path, file_info) in file_info_map.items():
                if original_ref in line:
                    embed_handled = True
                    parts = line.split(original_ref)

                    if parts[0].strip():
                        blocks.append(self._paragraph_block(self._convert_wikilinks(parts[0].strip())))

                    # Check if file was successfully uploaded (has file_upload_id)
                    if file_info and file_info.get("file_upload_id"):
                        file_block = self._file_block(file_info)
                        if file_block:
                            blocks.append(file_block)
                        else:
                            blocks.append(self._callout_block(
                                f"📎 Attachment: {file_info.get('name', file_path.name)}",
                                "gray_background"
                            ))
                    elif file_info:
                        # Fallback for failed uploads
                        blocks.append(self._callout_block(
                            f"📎 Attachment: {file_info.get('name', file_path.name)} (upload failed)",
                            "gray_background"
                        ))

                    after = original_ref.join(parts[1:]).strip()
                    if after:
                        blocks.append(self._paragraph_block(self._convert_wikilinks(after)))
                    break
            
            if embed_handled:
                i += 1
                continue
            
            # Headers
            if line.startswith('# '):
                blocks.append(self._heading_block(line[2:], 1))
            elif line.startswith('## '):
                blocks.append(self._heading_block(line[3:], 2))
            elif line.startswith('### '):
                blocks.append(self._heading_block(line[4:], 3))
            elif line.startswith('- ') or line.startswith('* '):
                blocks.append(self._bullet_block(self._convert_wikilinks(line[2:])))
            elif re.match(r'^\d+\. ', line):
                text = re.sub(r'^\d+\. ', '', line)
                blocks.append(self._numbered_block(self._convert_wikilinks(text)))
            elif line.startswith('- [ ] '):
                blocks.append(self._todo_block(self._convert_wikilinks(line[6:]), False))
            elif line.startswith('- [x] ') or line.startswith('- [X] '):
                blocks.append(self._todo_block(self._convert_wikilinks(line[6:]), True))
            elif line.startswith('> '):
                blocks.append(self._quote_block(self._convert_wikilinks(line[2:])))
            elif line.strip() in ['---', '***', '___']:
                blocks.append(self._divider_block())
            elif line.strip():
                converted_line = self._convert_wikilinks(line)
                blocks.append(self._paragraph_block(converted_line))
            
            i += 1
        
        if in_code_block and code_block_lines:
            blocks.append(self._code_block('\n'.join(code_block_lines), code_block_lang))
        
        return blocks
    
    def _convert_wikilinks(self, text: str) -> str:
        text = re.sub(r'\[\[([^\]|]+)\|([^\]]+)\]\]', r'\2', text)
        text = re.sub(r'\[\[([^\]]+)\]\]', r'\1', text)
        return text
    
    def _rich_text(self, text: str) -> list:
        if not text:
            return []

        segments = []

        # Step 1: Process markdown links [text](url)
        link_pattern = r'\[([^\]]+)\]\(([^)]+)\)'

        last_end = 0
        for match in re.finditer(link_pattern, text):
            # Add text before the link (will be processed for bare URLs)
            if match.start() > last_end:
                segments.extend(self._parse_text_with_urls(text[last_end:match.start()]))

            # Add the markdown link (validate URL first)
            link_text = match.group(1)
            link_url = self._sanitize_url(match.group(2))

            if link_url:
                segments.append({
                    "type": "text",
                    "text": {
                        "content": link_text[:2000],
                        "link": {"url": link_url}
                    }
                })
            else:
                # Invalid URL - just output the text without link
                segments.extend(self._parse_formatted_text(link_text))

            last_end = match.end()

        # Add remaining text after last markdown link
        if last_end < len(text):
            segments.extend(self._parse_text_with_urls(text[last_end:]))

        return segments if segments else [{"type": "text", "text": {"content": ""}}]

    def _parse_text_with_urls(self, text: str) -> list:
        """Parse text for bare URLs and convert them to links with titles."""
        if not text:
            return []

        segments = []
        last_end = 0

        for match in self.BARE_URL_PATTERN.finditer(text):
            # Strip trailing punctuation from matched URL
            raw_url = match.group(1).rstrip('.,;:!?)"\'')

            # Validate the URL
            url = self._sanitize_url(raw_url)
            if not url:
                # Invalid URL - skip this match, it will be processed as regular text
                continue

            # Add text before the URL
            if match.start() > last_end:
                segments.extend(self._parse_formatted_text(text[last_end:match.start()]))

            # Fetch title and create link
            title = self._fetch_page_title(url)
            link_text = title if title else url

            segments.append({
                "type": "text",
                "text": {
                    "content": link_text[:2000],
                    "link": {"url": url}
                }
            })

            # Account for any stripped punctuation - add it back as text
            url_end_in_match = match.start() + len(raw_url)
            last_end = url_end_in_match

        # Add remaining text after last URL
        if last_end < len(text):
            segments.extend(self._parse_formatted_text(text[last_end:]))

        # If no URLs were found, just parse the whole text for formatting
        if not segments:
            return self._parse_formatted_text(text)

        return segments

    def _parse_formatted_text(self, text: str) -> list:
        """Parse text for bold, italic, and code formatting."""
        if not text:
            return []

        segments = []
        pattern = r'(\*\*[^*]+\*\*|__[^_]+__|(?<!\*)\*[^*]+\*(?!\*)|_[^_]+_|`[^`]+`)'
        parts = re.split(pattern, text)

        for part in parts:
            if not part:
                continue

            annotations = {}
            content = part

            if part.startswith('**') and part.endswith('**'):
                content = part[2:-2]
                annotations['bold'] = True
            elif part.startswith('__') and part.endswith('__'):
                content = part[2:-2]
                annotations['bold'] = True
            elif part.startswith('*') and part.endswith('*') and len(part) > 2:
                content = part[1:-1]
                annotations['italic'] = True
            elif part.startswith('_') and part.endswith('_') and len(part) > 2:
                content = part[1:-1]
                annotations['italic'] = True
            elif part.startswith('`') and part.endswith('`'):
                content = part[1:-1]
                annotations['code'] = True

            while content:
                chunk = content[:2000]
                content = content[2000:]
                segment = {"type": "text", "text": {"content": chunk}}
                if annotations:
                    segment["annotations"] = annotations
                segments.append(segment)

        return segments
    
    def _paragraph_block(self, text: str) -> dict:
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": self._rich_text(text)}}
    
    def _heading_block(self, text: str, level: int) -> dict:
        heading_type = f"heading_{level}"
        return {"object": "block", "type": heading_type, heading_type: {"rich_text": self._rich_text(text)}}
    
    def _bullet_block(self, text: str) -> dict:
        return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": self._rich_text(text)}}
    
    def _numbered_block(self, text: str) -> dict:
        return {"object": "block", "type": "numbered_list_item", "numbered_list_item": {"rich_text": self._rich_text(text)}}
    
    def _quote_block(self, text: str) -> dict:
        return {"object": "block", "type": "quote", "quote": {"rich_text": self._rich_text(text)}}
    
    def _code_block(self, code: str, language: str) -> dict:
        lang_map = {"js": "javascript", "ts": "typescript", "py": "python", "rb": "ruby", 
                    "yml": "yaml", "sh": "shell", "bash": "shell", "zsh": "shell", "": "plain text"}
        language = lang_map.get(language.lower(), language.lower())
        return {"object": "block", "type": "code", "code": {"rich_text": [{"type": "text", "text": {"content": code[:2000]}}], "language": language}}
    
    def _divider_block(self) -> dict:
        return {"object": "block", "type": "divider", "divider": {}}
    
    def _todo_block(self, text: str, checked: bool) -> dict:
        return {"object": "block", "type": "to_do", "to_do": {"rich_text": self._rich_text(text), "checked": checked}}
    
    def _callout_block(self, text: str, color: str = "gray_background") -> dict:
        return {"object": "block", "type": "callout", "callout": {"rich_text": self._rich_text(text), "icon": {"type": "emoji", "emoji": "📎"}, "color": color}}
    
    def _file_block(self, file_info: dict) -> dict:
        file_type = file_info.get("type", "file")
        file_upload_id = file_info.get("file_upload_id")

        # Use file_upload reference for Notion-uploaded files
        if file_upload_id:
            file_obj = {"type": "file_upload", "file_upload": {"id": file_upload_id}}
        else:
            # Fallback - shouldn't happen if upload succeeded
            return None

        if file_type == "image":
            return {"object": "block", "type": "image", "image": file_obj}
        elif file_type == "video":
            return {"object": "block", "type": "video", "video": file_obj}
        elif file_type == "audio":
            return {"object": "block", "type": "audio", "audio": file_obj}
        elif file_type == "pdf":
            return {"object": "block", "type": "pdf", "pdf": file_obj}
        else:
            return {"object": "block", "type": "file", "file": file_obj}


# =============================================================================
# Notion Client Wrapper
# =============================================================================

class NotionMigrator:
    """Handles creating pages in Notion."""
    
    def __init__(self, config: Config):
        self.config = config
        self.client = NotionClient(auth=config.notion_token)
        self.created_pages = {}
    
    def create_page(self, parent_id: str, title: str, icon: str = None) -> str:
        if self.config.dry_run:
            fake_id = f"dry-run-{len(self.created_pages)}"
            logger.info(f"[DRY RUN] Would create page: {title}")
            self.created_pages[title] = fake_id
            return fake_id
        
        page_data = {
            "parent": {"page_id": parent_id},
            "properties": {"title": {"title": [{"text": {"content": title}}]}},
        }
        
        if icon:
            page_data["icon"] = {"type": "emoji", "emoji": icon}
        
        try:
            response = self.client.pages.create(**page_data)
            page_id = response["id"]
            self.created_pages[title] = page_id
            logger.info(f"Created page: {title}")
            return page_id
        except APIResponseError as e:
            logger.error(f"Failed to create page '{title}': {e}")
            raise
    
    def add_blocks(self, page_id: str, blocks: list):
        if self.config.dry_run:
            logger.info(f"[DRY RUN] Would add {len(blocks)} blocks")
            return
        
        if not blocks:
            return
        
        for i in range(0, len(blocks), 100):
            batch = blocks[i:i+100]
            try:
                self.client.blocks.children.append(block_id=page_id, children=batch)
                if i + 100 < len(blocks):
                    time.sleep(0.3)
            except APIResponseError as e:
                logger.error(f"Failed to add blocks: {e}")
                raise
        
        logger.debug(f"Added {len(blocks)} blocks to page")


# =============================================================================
# Migration Orchestrator
# =============================================================================

class MigrationOrchestrator:
    """Orchestrates the full migration process."""

    # Directory names to skip (attachment folders, hidden dirs)
    SKIP_DIRS = {'files', '.obsidian', '.trash', '.git'}

    # Icons for common folder names
    FOLDER_ICONS = {
        "journal": "📓", "journals": "📓",
        "area": "📋", "areas": "📋",
        "notes": "📝", "note": "📝",
        "resources": "📚", "resource": "📚",
        "archive": "🗄️", "archives": "🗄️",
        "reference": "📖", "references": "📖",
        "projects": "📂", "project": "📂",
        "inbox": "📥",
        "templates": "📋",
        "daily": "📅", "daily notes": "📅",
        "weekly": "📆",
    }

    def __init__(self, config: Config):
        self.config = config
        self.vault_path = config.vault_path

        self.notion = NotionMigrator(config)
        self.parser = ObsidianParser(self.vault_path)
        self.uploader = NotionFileUploader(config.notion_token, config)
        self.block_builder = NotionBlockBuilder(self.uploader)

        self.stats = {"directories": 0, "notes": 0, "files": 0, "errors": 0}

    def run(self):
        logger.info("=" * 60)
        logger.info("Obsidian to Notion Migration")
        logger.info("=" * 60)
        logger.info(f"Source: {self.vault_path}")
        logger.info(f"Destination page: {self.config.parent_page_id}")

        if self.config.dry_run:
            logger.info("MODE: Dry run (no changes will be made)")

        logger.info("-" * 60)

        # Migrate the source directory contents directly to the parent page
        self._migrate_directory_contents(self.vault_path, self.config.parent_page_id, depth=0)

        logger.info("=" * 60)
        logger.info("Migration Complete!")
        logger.info("-" * 60)
        logger.info(f"Directories:  {self.stats['directories']}")
        logger.info(f"Notes:        {self.stats['notes']}")
        logger.info(f"Files:        {self.stats['files']}")
        if self.stats['errors'] > 0:
            logger.warning(f"Errors:       {self.stats['errors']}")
        logger.info("=" * 60)

        # Write report of failed files
        self._write_failure_report()

        return True

    def _write_failure_report(self):
        """Write a report of files that couldn't be uploaded or resolved."""
        failed_uploads = self.uploader.failed_uploads
        successful_uploads = self.uploader.successful_uploads
        unresolved = self.parser.unresolved_references

        # Write CSV report of all files
        self._write_csv_report(successful_uploads, failed_uploads, unresolved)

        # Write text report if there were failures
        if not failed_uploads and not unresolved:
            return

        report_path = Path("migration_failed_files.txt")
        with open(report_path, 'w') as f:
            f.write("=" * 60 + "\n")
            f.write("Obsidian to Notion Migration - Failed Files Report\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 60 + "\n\n")

            if unresolved:
                f.write(f"UNRESOLVED FILE REFERENCES ({len(unresolved)})\n")
                f.write("-" * 40 + "\n")
                f.write("These files were referenced but could not be found:\n\n")
                for item in unresolved:
                    f.write(f"  Note: {item['note']}\n")
                    f.write(f"  Reference: {item['reference']}\n\n")

            if failed_uploads:
                f.write(f"\nFAILED UPLOADS ({len(failed_uploads)})\n")
                f.write("-" * 40 + "\n")
                f.write("These files were found but could not be uploaded:\n\n")
                for item in failed_uploads:
                    f.write(f"  File: {item['file']}\n")
                    f.write(f"  Reason: {item['reason']}\n\n")

        logger.info(f"Failure report written to: {report_path}")
        logger.warning(f"  - {len(unresolved)} unresolved file references")
        logger.warning(f"  - {len(failed_uploads)} failed uploads")

    def _write_csv_report(self, successful: list, failed: list, unresolved: list):
        """Write a CSV report of all file upload statuses."""
        csv_path = Path("migration_files_report.csv")

        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'file_path', 'file_name', 'status', 'notion_page_id',
                'notion_file_id', 'error_reason', 'referenced_from'
            ])

            # Successful uploads
            for item in successful:
                writer.writerow([
                    item['file'],
                    item['name'],
                    'uploaded',
                    item.get('parent_page_id', ''),
                    item.get('file_upload_id', ''),
                    '',
                    ''
                ])

            # Failed uploads
            for item in failed:
                writer.writerow([
                    item['file'],
                    item['name'],
                    'upload_failed',
                    '',
                    '',
                    item.get('reason', 'Unknown'),
                    ''
                ])

            # Unresolved references
            for item in unresolved:
                writer.writerow([
                    '',
                    item['reference'],
                    'not_found',
                    '',
                    '',
                    'File not found in vault',
                    item.get('note', '')
                ])

        logger.info(f"CSV report written to: {csv_path}")
        logger.info(f"  - {len(successful)} successful uploads")
        logger.info(f"  - {len(failed)} failed uploads")
        logger.info(f"  - {len(unresolved)} unresolved references")

    def _should_skip_dir(self, dir_path: Path) -> bool:
        """Check if a directory should be skipped."""
        name = dir_path.name.lower()
        return name.startswith('.') or name in self.SKIP_DIRS

    def _get_folder_icon(self, name: str) -> str:
        """Get an appropriate icon for a folder name."""
        return self.FOLDER_ICONS.get(name.lower(), "📁")

    def _migrate_directory_contents(self, dir_path: Path, parent_id: str, depth: int):
        """Recursively migrate a directory's contents."""
        # Get all subdirectories and markdown files (reverse order so newest/last appear first)
        subdirs = sorted(
            [d for d in dir_path.iterdir() if d.is_dir() and not self._should_skip_dir(d)],
            reverse=True
        )
        md_files = sorted(dir_path.glob("*.md"), reverse=True)

        # Migrate subdirectories first (they become subpages)
        for subdir in subdirs:
            self._migrate_directory(subdir, parent_id, depth)

        # Migrate markdown files
        for md_file in md_files:
            self._migrate_note(md_file, parent_id, depth)

    def _migrate_directory(self, dir_path: Path, parent_id: str, depth: int):
        """Migrate a directory as a Notion page with its contents as children."""
        logger.info(f"{'  ' * depth}📁 {dir_path.name}/")

        icon = self._get_folder_icon(dir_path.name)
        dir_page_id = self.notion.create_page(parent_id, dir_path.name, icon=icon)
        self.stats["directories"] += 1

        # Recursively migrate contents
        self._migrate_directory_contents(dir_path, dir_page_id, depth + 1)

    def _migrate_note(self, note_path: Path, parent_id: str, depth: int):
        """Migrate a markdown note as a Notion page."""
        logger.info(f"{'  ' * depth}📄 {note_path.name}")

        try:
            parsed = self.parser.parse_file(note_path)

            page_title = parsed.title
            if parsed.date:
                page_title = f"{parsed.date.strftime('%Y-%m-%d')} {parsed.title}".strip()

            note_page_id = self.notion.create_page(parent_id, page_title, icon="📄")

            # Build blocks with page_id so files can be uploaded to this note's page
            blocks = self.block_builder.build_blocks(parsed, note_path.parent, note_page_id)

            self.stats["files"] += len(parsed.file_references)

            if blocks:
                self.notion.add_blocks(note_page_id, blocks)

            self.stats["notes"] += 1

        except Exception as e:
            logger.error(f"{'  ' * depth}❌ Failed: {e}")
            self.stats["errors"] += 1
            if self.config.verbose:
                import traceback
                traceback.print_exc()


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    args = parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    source_path = Path(args.source).expanduser().resolve()
    if not source_path.exists():
        logger.error(f"Source path does not exist: {source_path}")
        sys.exit(1)
    
    if not source_path.is_dir():
        logger.error(f"Source path is not a directory: {source_path}")
        sys.exit(1)
    
    if not args.token:
        logger.error("Notion token required. Set NOTION_TOKEN env var or use --token flag")
        logger.info("Get your token at: https://www.notion.so/my-integrations")
        sys.exit(1)
    
    try:
        parent_page_id = extract_page_id(args.destination)
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)
    
    config = Config(
        vault_path=source_path,
        notion_token=args.token,
        parent_page_id=parent_page_id,
        dry_run=args.dry_run,
        skip_files=args.skip_files,
        verbose=args.verbose
    )
    
    orchestrator = MigrationOrchestrator(config)
    success = orchestrator.run()
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
