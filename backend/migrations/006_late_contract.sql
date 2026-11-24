CREATE TABLE IF NOT EXISTS renewal_contract_pending (
    offer_id integer PRIMARY KEY REFERENCES renewal_offer(id),
    contract_ref text NOT NULL UNIQUE,
    signed_on date NOT NULL,
    recorded_by integer NOT NULL REFERENCES users(id),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','booked')),
    booked_at timestamptz
);
