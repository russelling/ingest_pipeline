# Schema additions for ingest + turntable

**Verified against the live repo (russelling/FlowTrackingConfig @ main),
not assumed.** Fetching the actual `core/schema/project/assets/asset_type/asset/`
tree changed this significantly from earlier drafts of this pipeline:

- `source.yml` already exists there (`type: "static"`, comment: "raw scans,
  purchased assets, original deliverables") -- exactly what a vendor
  delivery is. **No new folder needed** for the raw ingest source; it uses
  the existing `asset_source_area` template/folder.
- `publish.yml` already exists (`type: "static"`, "single shared publish
  destination for all DCCs"). **No new folder needed** for the published
  USD either; it uses the existing `asset_publish_area`.
- `render.yml` and `review.yml` do **not** exist yet at the asset level --
  even though `core/templates.yml` already defines
  `unreal_asset_turntable_render`, `unreal_asset_turntable_flag`, and
  `unreal_asset_turntable_movie` pointing into `render/work/...` and
  `review/...`. That's a pre-existing gap in the repo (templates added
  before schema, which CLAUDE_INSTRUCTIONS.md's own rule 3 warns against) --
  not something this delivery introduces. Those templates are presumably
  unused/untested until this gap is filled.

So the only real schema addition needed is:

```
asset/
├── render/    [NEW static folder -- asset schema has no render/ level yet]
│   └── work/  [NEW static folder]
└── review/    [NEW static folder -- same as render/]
```

`.gitkeep` placeholders for `render/`, `render/work/`, and `review/` are
included in this delivery under the matching path in this folder, for you
to drop straight into
`core/schema/project/assets/asset_type/asset/`.

## Leftover folders in this delivery (ignore these)

Earlier drafts of this pipeline invented `work/ingest/`, `publish/ingest/`,
`work/turntable/`, and `review/turntable/` before the real repo structure
was confirmed. None of the four are used by the current scripts -- ingest
now reuses the existing `source/`/`publish/` folders, and turntable
rendering now writes into the pre-existing (if not-yet-schema'd)
`render/work/{Asset}_turntable_v{version}/` and `review/` paths via the
templates that already exist in `core/templates.yml`. This delivery still
contains `.gitkeep`s for those four folders as leftovers; they're harmless
if merged (Toolkit just won't ever populate them) but you can skip them.

If your Toolkit folder schema uses per-folder `.yml` definition files
(rather than bare directories defaulting to `type: static`), the pattern
to copy from is `publish.yml` / `source.yml` themselves -- both are just:

```yaml
type: "static"
```
