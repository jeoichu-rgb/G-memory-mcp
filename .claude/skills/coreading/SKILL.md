---
name: coreading
description: "Co-reading / shared reading surface for books. Use whenever the user mentions reading a book, continuing reading, checking reading progress, annotations, margin notes, importing a book (EPUB/TXT), searching book passages, or anything related to the shared reading library. Also trigger when the user references book titles, chapter navigation, or wants to discuss specific passages."
---

# Co-Reading Skill

Jeoi and Erik's shared reading library. All operations go through a JSON-RPC POST endpoint.

## API Endpoint

```
POST https://read.erikssheep.uk/mcp
Content-Type: application/json; charset=utf-8
Authorization: Bearer Jeoi2026
```

Alternatively, simple reads can use the REST API:
```
GET https://read.erikssheep.uk/api/books          — list all books
```

## Calling Tools

Use Bash with curl. Every call follows this pattern:

```bash
curl -s -X POST "https://read.erikssheep.uk/mcp" \
  -H "Content-Type: application/json; charset=utf-8" \
  -H "Authorization: Bearer Jeoi2026" \
  --data-binary @- << 'EOF'
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"TOOL_NAME","arguments":{...}}}
EOF
```

Use `--data-binary @-` with a heredoc to ensure Chinese characters are preserved correctly. Always use single-quoted heredoc (`<< 'EOF'`) to prevent shell expansion.

## Available Tools

### Reading

| Tool | Required Args | Optional Args | Description |
|------|--------------|---------------|-------------|
| `reading_list_books` | — | — | List all books with progress |
| `reading_list_chunks` | `bookId` | — | List chunks in reading order |
| `reading_read_chunk` | `bookId`, `chunkId` | — | Read one chunk, returns prevId/nextId |
| `reading_continue` | — | `bookId` | Continue from next unread chunk (defaults to most recent book) |
| `reading_search_chunks` | `query` | `bookId`, `limit` | Full-text search across chunks |
| `reading_get_progress` | — | `bookId` | Reading progress for one or all books |
| `reading_mark_read` | `bookId`, `chunkId` | — | Mark a chunk as read |

### Annotations

| Tool | Required Args | Optional Args | Description |
|------|--------------|---------------|-------------|
| `reading_annotate_passage` | `bookId`, `chunkId`, `quote`, `note` | `author`, `kind`, `mood`, `tags[]`, `status`, `parentId` | Write a margin annotation anchored to a quote |
| `reading_list_annotations` | — | `bookId`, `chunkId`, `kind`, `author`, `status`, `parentId` | List/filter annotations |
| `reading_reply_to_annotation` | `parentId`, `note` | `author`, `kind`, `mood`, `tags[]`, `bookId`, `chunkId`, `quote` | Reply under an existing annotation |
| `reading_submit_user_notes` | — | `bookId`, `chunkId`, `sessionId`, `contextMode` | Submit open user notes for review |

### Import

| Tool | Required Args | Optional Args | Description |
|------|--------------|---------------|-------------|
| `reading_import_book` | `filename`, `dataBase64` | `format`, `bookId`, `title`, `author`, `maxChars`, `overwrite` | Import small EPUB/TXT in one request |
| `reading_import_begin` | `filename` | `format`, `expectedBytes`, `bookId`, `title`, `author`, `maxChars`, `overwrite` | Start chunked import for large files |
| `reading_import_part` | `uploadId`, `dataBase64` | `index` | Append one part to active import |
| `reading_import_finish` | `uploadId` | — | Finalize chunked import |
| `reading_import_cancel` | `uploadId` | — | Cancel and clean up chunked import |

## Usage Patterns

**Continue reading the current book:**
```bash
curl -s -X POST "https://read.erikssheep.uk/mcp" \
  -H "Content-Type: application/json; charset=utf-8" \
  -H "Authorization: Bearer Jeoi2026" \
  --data-binary @- << 'EOF'
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"reading_continue","arguments":{}}}
EOF
```

**Leave an annotation:**
```bash
curl -s -X POST "https://read.erikssheep.uk/mcp" \
  -H "Content-Type: application/json; charset=utf-8" \
  -H "Authorization: Bearer Jeoi2026" \
  --data-binary @- << 'EOF'
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"reading_annotate_passage","arguments":{"bookId":"汉尼拔","chunkId":"ch23","quote":"原文引用","note":"Erik的批注"}}}
EOF
```

After reading a chunk, always call `reading_mark_read` to update progress.

When Jeoi says "继续看书" or "读下一章", use `reading_continue`. When discussing a passage, use `reading_search_chunks` to locate it, then `reading_read_chunk` to get the full text.
