CREATE TABLE IF NOT EXISTS dispatch_inbox (
  organization_id text NOT NULL,
  dispatch_id text NOT NULL,
  conversation_id text NOT NULL,
  payload jsonb NOT NULL,
  state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','started','handoff','done')),
  owner text,
  available_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organization_id,dispatch_id)
);
CREATE INDEX IF NOT EXISTS dispatch_inbox_due_idx ON dispatch_inbox(available_at) WHERE state <> 'done';
