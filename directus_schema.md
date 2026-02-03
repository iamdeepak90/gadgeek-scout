# Directus schema (recommended)

This project expects **three** collections:

## 1) `news_leads` (leads queue)

Fields:
- `id` (auto)
- `title` (string) **required**
- `source_url` (string) **required**, **unique** (used for dedupe)
- `category_slug` (string) **required**
- `status` (string) **required**: `pending` | `approved` | `rejected` | `published`

Notes:
- Dedupe is ONLY against `news_leads.source_url`.

## 2) `Articles` (published content)

Fields:
- `id` (auto)
- `title` (string) required
- `slug` (string) required, unique recommended
- `status` (string) required: `published` / `draft`
- `category_slug` (string) required
- `short_description` (text)
- `content` (long text) HTML
- `featured_image` (string) (URL or data URI if you choose base64)
- `featured_image_credit` (string)
- `featured_image_alt` (string)
- `meta_title` (string)
- `meta_description` (string)
- `tags` (JSON)
- `published_at` (datetime)

## 3) `categories` (read-only from UI, managed in Directus)

Fields:
- `id` (auto)
- `slug` (string) **required**, unique
- `name` (string) **required**
- `priority` (int) 1..n (lower = higher priority)
- `posts_per_scout` (int) number of leads per scout run for this category
- `keywords` (JSON array of strings) - used for keyword matching
- `enabled` (boolean)

Matching logic:
- If RSS feed has `category_hint` matching a category `slug`, it's used.
- Otherwise the scout scores `title + description + content` by keyword presence.

