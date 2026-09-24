CREATE TABLE delivery_backoff (
 id BOOLEAN PRIMARY KEY DEFAULT true CHECK(id),
 blocked_until TIMESTAMPTZ NOT NULL
);
