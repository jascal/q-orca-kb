# machine PaperIndexing

> Workflow for fetching a paper from arXiv, extracting its text,
> chunking it, and indexing it into a mempalace knowledge base.

## context

| Field         | Type   | Default |
|---------------|--------|---------|
| arxiv_id      | string | ""      |
| wing          | string | ""      |
| pdf_path      | string | ""      |
| text          | string | ""      |
| chunk_count   | int    | 0       |
| indexed_count | int    | 0       |
| attempts      | int    | 0       |
| max_attempts  | int    | 3       |
| error         | string | ""      |

## events

- start
- fetch_ok
- fetch_failed
- extract_ok
- extract_failed
- index_ok
- index_failed
- retry
- give_up

## effects

| Name          | Input                                              | Output                                     |
|---------------|----------------------------------------------------|--------------------------------------------|
| FetchArxiv    | `{ arxiv_id: string }`                             | `{ pdf_path: string }`                     |
| ExtractText   | `{ pdf_path: string }`                             | `{ text: string }`                         |
| IndexInPalace | `{ wing: string, arxiv_id: string, text: string }` | `{ chunk_count: int, indexed_count: int }` |

## state idle [initial]
> Awaiting a start event with arxiv_id and wing in the payload.
- ignore: *

## state fetching
> Downloading PDF from arXiv via the FetchArxiv effect.
- ignore: *

## state extracting
> Extracting text from the downloaded PDF via ExtractText.
- ignore: *

## state indexing
> Chunking and upserting drawers into mempalace via IndexInPalace.
- ignore: *

## state failed
> A step failed and we are deciding whether to retry or give up.
- ignore: *

## state done [final]
> Paper successfully indexed into the palace.

## state aborted [final]
> Indexing abandoned after exceeding max retry attempts.

## transitions

| Source     | Event          | Guard               | Target     | Action            |
|------------|----------------|---------------------|------------|-------------------|
| idle       | start          |                     | fetching   | begin_fetch       |
| fetching   | fetch_ok       |                     | extracting | begin_extract     |
| fetching   | fetch_failed   |                     | failed     | record_error      |
| extracting | extract_ok     |                     | indexing   | begin_index       |
| extracting | extract_failed |                     | failed     | record_error      |
| indexing   | index_ok       |                     | done       | record_indexed    |
| indexing   | index_failed   |                     | failed     | record_error      |
| failed     | retry          | can_retry           | fetching   | bump_attempts     |
| failed     | give_up        | !can_retry          | aborted    | finalize_failure  |

## guards

| Name      | Expression                |
|-----------|---------------------------|
| can_retry | `attempts < max_attempts` |

## actions

| Name             | Signature          | Effect        |
|------------------|--------------------|---------------|
| begin_fetch      | `(ctx) -> Context` | FetchArxiv    |
| begin_extract    | `(ctx) -> Context` | ExtractText   |
| begin_index      | `(ctx) -> Context` | IndexInPalace |
| record_indexed   | `(ctx) -> Context` |               |
| record_error     | `(ctx) -> Context` |               |
| bump_attempts    | `(ctx) -> Context` |               |
| finalize_failure | `(ctx) -> Context` |               |
