-- All data are synthetic. Permissions and operative evidence are public reads.
CREATE TABLE crm_clients (
  client_id TEXT PRIMARY KEY, name TEXT NOT NULL, domain TEXT NOT NULL UNIQUE,
  email TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','suspended')),
  revision INTEGER NOT NULL CHECK(revision > 0)
);
CREATE TABLE crm_contacts (
  contact_id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES crm_clients,
  first_name TEXT NOT NULL, last_name TEXT NOT NULL, email TEXT NOT NULL COLLATE NOCASE UNIQUE,
  title TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE crm_opportunities (
  opportunity_id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES crm_clients,
  name TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES users,
  stage TEXT NOT NULL CHECK(stage IN ('discovery','qualified','proposal','negotiation','won','lost')),
  probability INTEGER NOT NULL CHECK(probability BETWEEN 0 AND 100),
  amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0), currency TEXT NOT NULL CHECK(currency IN ('USD','EUR')),
  revision INTEGER NOT NULL CHECK(revision > 0)
);
CREATE TABLE crm_quotes (
  quote_id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES crm_opportunities,
  amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0), currency TEXT NOT NULL CHECK(currency IN ('USD','EUR')),
  discount_bps INTEGER NOT NULL CHECK(discount_bps BETWEEN 0 AND 10000),
  status TEXT NOT NULL CHECK(status IN ('draft','approved','signed','cancelled')),
  valid_until TEXT NOT NULL, version INTEGER NOT NULL CHECK(version > 0),
  predecessor_id TEXT REFERENCES crm_quotes, approval_id TEXT,
  UNIQUE(opportunity_id, version)
);
CREATE UNIQUE INDEX crm_one_open_quote ON crm_quotes(opportunity_id) WHERE status IN ('draft','approved');
CREATE TABLE crm_quote_lines (
  quote_id TEXT NOT NULL REFERENCES crm_quotes, sku TEXT NOT NULL,
  quantity INTEGER NOT NULL CHECK(quantity > 0), unit_minor INTEGER NOT NULL CHECK(unit_minor >= 0),
  price_revision INTEGER NOT NULL CHECK(price_revision > 0), PRIMARY KEY(quote_id, sku)
);
CREATE TABLE crm_contracts (
  contract_id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES crm_clients,
  opportunity_id TEXT NOT NULL REFERENCES crm_opportunities, quote_id TEXT NOT NULL REFERENCES crm_quotes,
  value_minor INTEGER NOT NULL CHECK(value_minor >= 0), currency TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status = 'draft'), signed_at TEXT CHECK(signed_at IS NULL),
  UNIQUE(quote_id)
);
CREATE TABLE crm_documents (
  document_id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES crm_clients,
  url TEXT NOT NULL UNIQUE, file_name TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('approved','superseded')),
  asset_id TEXT NOT NULL REFERENCES evidence_files
);
CREATE TABLE crm_attachments (
  attachment_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES crm_documents,
  opportunity_id TEXT NOT NULL REFERENCES crm_opportunities, file_name TEXT NOT NULL,
  UNIQUE(document_id, opportunity_id)
);
CREATE TABLE crm_notes (
  note_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL CHECK(entity_type IN ('client','opportunity')),
  entity_id TEXT NOT NULL, content TEXT NOT NULL CHECK(length(trim(content)) > 0),
  reference_ids_json TEXT NOT NULL, evidence_ids_json TEXT NOT NULL
);
CREATE TABLE crm_requests (
  request_id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES crm_clients,
  thread_id TEXT NOT NULL, sequence INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('current','superseded')),
  sender_id TEXT NOT NULL REFERENCES users, subject TEXT NOT NULL, body TEXT NOT NULL,
  asset_id TEXT NOT NULL REFERENCES evidence_files, UNIQUE(thread_id, sequence)
);
CREATE TABLE crm_policies (
  policy_id TEXT PRIMARY KEY, topic TEXT NOT NULL, revision INTEGER NOT NULL,
  effective_from TEXT NOT NULL, effective_until TEXT NOT NULL, rules_json TEXT NOT NULL,
  asset_id TEXT NOT NULL REFERENCES evidence_files
);
CREATE TABLE crm_prices (
  sku TEXT NOT NULL, revision INTEGER NOT NULL, currency TEXT NOT NULL,
  unit_minor INTEGER NOT NULL CHECK(unit_minor >= 0), valid_from TEXT NOT NULL, valid_until TEXT NOT NULL,
  PRIMARY KEY(sku, revision, currency)
);
CREATE TABLE crm_approvals (
  approval_id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES crm_opportunities,
  currency TEXT NOT NULL, max_discount_bps INTEGER NOT NULL CHECK(max_discount_bps BETWEEN 0 AND 10000),
  max_gross_minor INTEGER NOT NULL CHECK(max_gross_minor >= 0),
  expires_on TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('approved','revoked')),
  asset_id TEXT NOT NULL REFERENCES evidence_files
);
CREATE TABLE crm_permissions (
  permission_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users,
  operation TEXT NOT NULL, target_id TEXT NOT NULL, limit_minor INTEGER NOT NULL,
  valid_from TEXT NOT NULL, valid_until TEXT NOT NULL, UNIQUE(user_id, operation, target_id)
);
CREATE TABLE crm_evidence (
  asset_id TEXT PRIMARY KEY REFERENCES evidence_files, content TEXT NOT NULL
);
