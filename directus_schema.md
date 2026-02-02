# Directus Schema (Recommended)

This project assumes **three** collections:

1) `categories` (recommended)
2) `news_leads` (approval queue)
3) `Articles` (published posts)

You can rename collections/fields, but then update `config.py` field mappings.

---

## 1) Collection: `categories` (recommended)

**Purpose:** canonical list of your Google News-aligned categories.

### Fields
| Field | Type | Required | Unique | Notes |
|---|---|---:|---:|---|
| `id` | UUID (auto) or Integer (auto) | ✅ | ✅ | Primary key |
| `slug` | String | ✅ | ✅ | e.g. `smartphones` |
| `name` | String | ✅ | ❌ | e.g. `Smartphones` |
| `emoji` | String | ❌ | ❌ | optional |
| `priority` | Integer | ✅ | ❌ | 1–3 |

### Indexes / constraints
- Unique: `slug`

### Optional seed data
Create 10 categories (or 12 if you keep tier-3):
- smartphones
- phone-reviews
- laptops-pcs
- ai-software
- gaming
- buying-guides
- comparisons
- wearables
- tech-industry
- privacy-security
- chips-silicon (optional but recommended)
- smart-home-iot (optional but recommended)
- leaks-rumors (tier-3)
- tech-events (tier-3)

---

## 2) Collection: `news_leads`

**Purpose:** holds discovered topics and Slack approval workflow.

### Fields (recommended)
| Field | Type | Required | Unique | Notes |
|---|---|---:|---:|---|
| `id` | Auto | ✅ | ✅ | Primary key |
| `title` | String | ✅ | ❌ | RSS/News title |
| `source_url` | String | ✅ | ❌ | canonicalized |
| `source_domain` | String | ✅ | ❌ | derived |
| `category_slug` | String OR M2O → categories | ✅ | ❌ | keep string if you want simplicity |
| `status` | String | ✅ | ❌ | enum below |
| `priority` | Integer | ❌ | ❌ | 0 urgent, 1–3 normal |
| `fingerprint` | String | ✅ | ✅ | sha256(title|url) |
| `matched_keywords` | JSON | ❌ | ❌ | array of keywords matched |
| `source_published_at` | String | ❌ | ❌ | raw feed string |
| `discovered_at` | DateTime | ✅ | ❌ | default now |
| `slack_ts` | String | ❌ | ❌ | Slack message timestamp |
| `slack_channel` | String | ❌ | ❌ | Slack channel id |
| `approved_by` | String | ❌ | ❌ | Slack username |
| `approved_at` | DateTime | ❌ | ❌ | optional |
| `last_error` | Text | ❌ | ❌ | last processing error |

### Status enum
Use these exact strings (or update config.py):
- `pending`
- `queued`
- `processing`
- `processed`
- `rejected`
- `failed`

### Indexes / constraints
- Unique: `fingerprint`
- Index: `status`, `discovered_at`

### Permissions (recommended)
- **Public role:** No access
- **Service role (static token):** read/write all

---

## 3) Collection: `Articles`

**Purpose:** stores published content.

### Fields (recommended)
| Field | Type | Required | Unique | Notes |
|---|---|---:|---:|---|
| `id` | Auto | ✅ | ✅ | Primary |
| `status` | String | ✅ | ❌ | `draft` / `published` |
| `title` | String | ✅ | ❌ | |
| `slug` | String | ✅ | ✅ | |
| `short_description` | Text | ✅ | ❌ | intro summary |
| `content` | Long Text | ✅ | ❌ | HTML content |
| `category_slug` | String OR M2O → categories | ✅ | ❌ | |
| `source_url` | String | ✅ | ❌ | original seed URL |
| `sources` | JSON | ✅ | ❌ | list of URLs used |
| `featured_image` | String | ❌ | ❌ | image URL |
| `featured_image_alt` | String | ❌ | ❌ | |
| `featured_image_credit` | Text | ❌ | ❌ | attribution |
| `focus_keyword` | String | ❌ | ❌ | SEO keyword |
| `tags` | JSON | ❌ | ❌ | array |
| `meta_title` | String | ❌ | ❌ | |
| `meta_description` | Text | ❌ | ❌ | |
| `word_count` | Integer | ❌ | ❌ | |
| `fingerprint` | String | ✅ | ✅ | same as lead fingerprint |
| `published_at` | DateTime | ❌ | ❌ | default now (or let Directus manage) |

### Indexes / constraints
- Unique: `slug`, `fingerprint`
- Optional unique: `source_url` (if you want strict)

### Permissions (recommended)
- **Public role:**
  - read: only `status = "published"`
- **Service role (static token):**
  - read/write all

---

## Directus Settings / Operational Recommendations

### API access token
Create a **static token** for the automation user and put it in `config.py`:
Settings → Access Tokens → Create Token

### CORS
If your frontend is separate:
- enable CORS for your domain (Directus Settings)

### Hooks / Flows (optional)
- When an `Articles` item becomes `published`, trigger:
  - front-end rebuild hook (if needed)
  - ping sitemap generator (optional)

### Content fields
Use HTML storage for `content`. Most frontends render it directly.

### Google News readiness
- Keep bylines and publish dates visible on your frontend.
- Ensure your site has clear publisher info (About/Contact pages).
- Maintain a News sitemap (outside this codebase).

