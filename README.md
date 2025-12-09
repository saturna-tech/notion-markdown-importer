# Markdown Import Tool for Notion

Migrate your markdown workspace to Notion with page hierarchy and embedded
files.  For example, this could import an Obsidial Vault to Notion.

Author: Ryan Cabeen, ryan@saturnatech.com

## Features

- Preserves any folder structure as nested Notion pages
- Parses Obsidian-flavored markdown
- Uploads files directly to Notion via the file upload API
- Embeds images, PDFs, videos, and audio inline in notes
- Converts markdown links `[text](url)` to clickable Notion links
- Auto-fetches page titles for bare URLs
- Extracts dates from `YYYY-MM-DD Title.md` filenames
- Sorts in reverse order (newest first for timestamped notes)
- Generates CSV report of all file upload statuses
- Dry-run mode to preview changes

## Quick Start

### 1. Install

```bash
pip install notion-client requests
```

### 2. Create Notion Integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Click **+ New integration**
3. Name it (e.g., "Obsidian Migration")
4. Enable **Read content** capability (required for file uploads)
5. Copy the **Internal Integration Token**

### 3. Share Target Page

1. Open your destination page in Notion
2. Click **⋯** → **Add connections** → Select your integration

### 4. Run

```bash
# Set token as environment variable
export NOTION_TOKEN="secret_xxxxxxxxxx"

# Run migration
python migrate.py ~/Documents/MyVault "https://www.notion.so/myteam/Page-abc123"
```

Or pass token directly:

```bash
python migrate.py ~/Documents/MyVault "https://notion.so/Page-abc123" --token secret_xxx
```

## Usage

```
python migrate.py SOURCE DESTINATION [OPTIONS]

Arguments:
  SOURCE        Path to Obsidian vault or any directory to migrate
  DESTINATION   Notion page URL where content will be created

Options:
  --token TOKEN    Notion integration token (default: NOTION_TOKEN env var)
  --dry-run        Preview migration without making changes
  --skip-files     Skip file uploads (migrate notes only)
  --verbose, -v    Enable verbose logging
```

## Examples

```bash
# Migrate entire vault
python migrate.py ~/Obsidian/MyVault "https://notion.so/myteam/abc123"

# Migrate just a subfolder
python migrate.py ~/Obsidian/MyVault/Projects "https://notion.so/abc123"

# Preview what will happen
python migrate.py ~/Obsidian/Work "https://notion.so/myteam/abc123" --dry-run

# Skip file uploads (faster, notes only)
python migrate.py ~/Obsidian "https://notion.so/abc123" --skip-files

# Verbose output for debugging
python migrate.py ~/Obsidian "https://notion.so/abc123" -v
```

## Directory Structure

The script works with **any directory structure**. It recursively processes all folders and markdown files:

```
AnyFolder/
├── Subfolder A/
│   ├── files/              ← attachments (auto-skipped as folder)
│   │   └── diagram.png
│   ├── 2024-12-01 Note.md
│   └── 2024-11-15 Note.md
├── Subfolder B/
│   └── Overview.md
└── README.md
```

**Auto-skipped directories:** `files/`, `.obsidian/`, `.git/`, `.trash/`, and any hidden folders (starting with `.`)

## Result in Notion

```
📁 Subfolder A
│   ├── 📄 2024-12-01 Note      ← newest first
│   │   └── [diagram.png embedded]
│   └── 📄 2024-11-15 Note
📁 Subfolder B
│   └── 📄 Overview
📄 README
```

Folders get contextual icons based on name (📓 Journal, 📋 Areas, 📚 Resources, etc.)

## File Handling

### Supported Embed Syntax

| Syntax | Example |
|--------|---------|
| Obsidian embed | `![[diagram.png]]` |
| Markdown image | `![alt](path/to/image.png)` |
| Markdown file link | `[Document](path/to/file.pdf)` |

### File Resolution Order

When looking for a referenced file:

1. `files/` subdirectory next to the note
2. Same directory as the note
3. Vault root
4. **Fallback:** Search entire directory tree for matching filename

URL-encoded paths (e.g., `path%20with%20spaces`) are automatically decoded.

### Supported File Types

- **Images:** png, jpg, jpeg, gif, webp, svg, bmp, ico, tiff
- **Documents:** pdf, doc, docx, xls, xlsx, ppt, pptx, txt, csv, html
- **Video:** mp4, mov, webm, avi, mkv
- **Audio:** mp3, wav, ogg, m4a, flac
- **Archives:** zip, tar, gz, rar, 7z
- **Code:** json, xml, yaml, yml, ipynb

## Link Handling

### Markdown Links
`[Link Text](https://example.com)` → Clickable Notion link with "Link Text"

### Bare URLs
`https://example.com` → Clickable link with auto-fetched page title

### Wiki Links
`[[Note Name]]` → Plain text (Notion API doesn't support cross-page links)

## Markdown Support

| Obsidian | Notion |
|----------|--------|
| `# Heading` | Heading 1 |
| `## Heading` | Heading 2 |
| `### Heading` | Heading 3 |
| `- bullet` | Bulleted list |
| `1. numbered` | Numbered list |
| `- [ ] todo` | To-do (unchecked) |
| `- [x] done` | To-do (checked) |
| `> quote` | Quote block |
| `` ```code``` `` | Code block |
| `**bold**` | Bold |
| `*italic*` | Italic |
| `` `code` `` | Inline code |
| `---` | Divider |
| `![[file]]` | Embedded file |
| `[text](url)` | Clickable link |

## Reports

After migration, the script generates:

### `migration_files_report.csv`

CSV with every file's status:

| Column | Description |
|--------|-------------|
| `file_path` | Full path to source file |
| `file_name` | Filename |
| `status` | `uploaded`, `upload_failed`, or `not_found` |
| `notion_page_id` | Notion page where file was uploaded |
| `notion_file_id` | Notion file upload ID |
| `error_reason` | Why it failed (if applicable) |
| `referenced_from` | Which note referenced it |

### `migration_failed_files.txt`

Human-readable report of failures (only created if there were issues):

- **Unresolved references:** Files mentioned in notes but not found
- **Failed uploads:** Files found but couldn't be uploaded (with error details)

## Troubleshooting

### "Could not find integration"
Share your destination page with the integration: **⋯** → **Add connections**

### "401 Unauthorized"
Check that your `NOTION_TOKEN` is correct

### "Invalid URL for link"
Some URLs in your notes may be malformed. Run with `-v` to see which URLs are being skipped.

### Files not appearing
- Verify your Notion integration has **Read content** capability enabled
- Check the `migration_files_report.csv` for specific errors
- Use `--verbose` to see detailed upload logs

### Files not found
- Check `migration_failed_files.txt` for unresolved references
- The script searches the entire directory tree as a fallback
- Verify the file exists and the path/filename matches

### Rate limiting
The script includes delays between API calls. For very large vaults, you may need to run in batches.

## Limitations

- **Internal links:** `[[Note Name]]` becomes plain text (Notion API limitation)
- **Dataview queries:** Not converted (use Notion databases instead)
- **Plugins:** Plugin-specific syntax won't transfer
- **File size:** Notion has file size limits for API uploads 
- **Inline files:** Files in Notion are block-level, so inline file links become separate blocks
