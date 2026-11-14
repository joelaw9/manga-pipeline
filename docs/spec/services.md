# manga_pipeline spec — Services

Part of the spec; the index is [`SPEC.md`](../../SPEC.md). Update this file in the same change as the behaviour it describes.

## Services

The pipeline talks to Suwayomi (GraphQL) and Kavita (REST) through 1 HTTP layer, which every later service (the metadata sources) uses too. A run has 1 client of each service, so what a client learns (Kavita's token, a service found unreachable) holds for the whole run.

- A request that ends in a network error, a timeout, HTTP 429 or a status of 500 or above is sent again, up to 4 times, after 2, 4, 8 and 16 seconds, or after the seconds its `Retry-After` header names.
- A service is unreachable (SPEC "Parts") when a request still ends, after those retries, in a network error, a timeout or HTTP 429, 502, 503 or 504, or as soon as it answers with a `Retry-After` above 60 seconds. A run that found a service unreachable does not ask it again.
- Any other status of 400 or above is a failure of the request: one of 500 or above (HTTP 500 included) after its retries, one below 500 at once.
- A service can have a minimum gap between 2 requests; Suwayomi and Kavita have none.
- Every request is logged at DEBUG. No log line or error message carries a secret: the value of a query parameter whose name contains `key`, `token`, `secret`, `pass`, `auth` or `sig`, in any case, is replaced.

`smoke` checks both services with reads only: it prints the size of Suwayomi's library, its source count and 1 manga's chapters, then Kavita's libraries, the series count of `kavita.library_id` and 1 series' files after path mapping, with whether each mapped path exists; a mapped path that does not exist fails the check. It tries both services whatever the first one answers, and exits 1 when either check fails.

### Suwayomi

- The API takes no credentials.
- A GraphQL reply that carries `errors` is a failure, though its HTTP status is 200.
- 2 calls make Suwayomi reach a download source: searching a source, and fetching a manga's chapter list. In those 2, an error raised while the query ran (it carries a `path`) means the source is unavailable, not that the call failed; an error without a `path` (the query itself is wrong) is a failure. A timeout of those 2 is not retried: Suwayomi is asked a trivial query, and when it answers, the source is unavailable; when it does not, Suwayomi is unreachable.
- The library is read 50 manga at a time, in id order, each query starting after the last id read (a manga added or removed meanwhile shifts nothing), and chapters per manga; the whole library is never asked for in 1 query.
- A manga Suwayomi does not hold is reported as absent, not as an error.
- A download lies at `<source display name>/<manga title>/<chapter>.cbz`, where `<chapter>` is `<scanlator>_<chapter name>`, or the chapter name alone when the chapter has no scanlator. Each of the 3 parts is made a valid file name the way Suwayomi does it: leading and trailing dots and spaces are removed (nothing left gives `(invalid)`); control characters, DEL and `" * / : < > ? \ |` become `_`; the result is cut to 240 UTF-16 units and 240 UTF-8 bytes, never inside a character.

### Kavita

- The API key is exchanged for a token, kept for the run. A call answered 401 gets 1 new token and 1 more try; a second 401 makes Kavita unreachable, with 1 warning.
- A series Kavita answers with 404 or an empty reply is gone.
- Kavita reports file paths in its own mount. `paths.kavita_path_map` (Kavita path prefix to local path prefix) turns each into a local path: the longest prefix that ends on a path boundary wins, case counts, and a path under no prefix is an error. A prefix counts without its trailing `/`; 2 entries giving the same prefix are refused (SPEC "Preflight"). Every path from Kavita goes through it.
- A series' folder is the single folder that holds its files, never the folder Kavita records for the series (for a series under a shared top folder that is the top folder). A series whose files lie in no folder, in 2 or more, or directly in one of a library's own folders has no usable folder.
- A library scan is requested, never awaited: Kavita runs them later, so a file just published is not indexed when the request returns.
- A library's series are listed in name order.
