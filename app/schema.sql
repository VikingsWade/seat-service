CREATE TABLE IF NOT EXISTS shows (
    id              uuid PRIMARY KEY,
    name            text NOT NULL,
    price_paise     bigint NOT NULL CHECK (price_paise >= 0),
    per_user_limit  integer NOT NULL CHECK (per_user_limit >= 1),
    total_seats     integer NOT NULL CHECK (total_seats >= 1),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reservations (
    id            uuid PRIMARY KEY,
    show_id       uuid NOT NULL REFERENCES shows (id),
    user_id       text NOT NULL,
    seats         text[] NOT NULL,
    amount_paise  bigint NOT NULL,
    status        text NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    cancelled_at  timestamptz
);
CREATE INDEX IF NOT EXISTS reservations_user_idx ON reservations (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS seats (
    show_id         uuid NOT NULL REFERENCES shows (id) ON DELETE CASCADE,
    label           text COLLATE "C" NOT NULL,
    status          text NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'held', 'confirmed')),
    user_id         text,
    reservation_id  uuid REFERENCES reservations (id),
    PRIMARY KEY (show_id, label),
    CHECK (
        (status = 'available' AND user_id IS NULL AND reservation_id IS NULL)
        OR (status <> 'available' AND user_id IS NOT NULL AND reservation_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS seats_owner_idx ON seats (show_id, user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS seats_reservation_idx ON seats (reservation_id) WHERE reservation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS idempotency_keys (
    user_id         text NOT NULL,
    key             text NOT NULL,
    request_hash    text NOT NULL,
    reservation_id  uuid NOT NULL REFERENCES reservations (id),
    response        jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);
