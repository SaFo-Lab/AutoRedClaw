-- current schema snapshot (pre-migration)
CREATE TABLE users (id BIGSERIAL PRIMARY KEY, email VARCHAR(255) NOT NULL UNIQUE, full_name VARCHAR(255));
CREATE TABLE orders (id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(id), total NUMERIC(12,2));
