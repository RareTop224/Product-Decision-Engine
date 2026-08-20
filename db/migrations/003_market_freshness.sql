CREATE TABLE IF NOT EXISTS pde.product_lifecycle_observations (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES pde.products(id),
    source_provider_key TEXT NOT NULL REFERENCES pde.data_providers(provider_key),
    lifecycle_status TEXT NOT NULL CHECK (
        lifecycle_status IN ('active', 'discontinued', 'unknown')
    ),
    observed_at DATE NOT NULL,
    source_url TEXT,
    verification_status TEXT NOT NULL CHECK (
        verification_status IN ('candidate', 'verified', 'conflict', 'rejected')
    ),
    notes TEXT,
    last_import_run_id UUID REFERENCES pde.import_runs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS product_lifecycle_observation_lookup_idx
    ON pde.product_lifecycle_observations(
        product_id, source_provider_key, observed_at DESC
    );

CREATE OR REPLACE VIEW pde.latest_product_lifecycle AS
SELECT DISTINCT ON (observation.product_id, observation.source_provider_key)
    observation.product_id,
    observation.source_provider_key,
    observation.observed_at,
    observation.lifecycle_status,
    observation.source_url,
    observation.verification_status,
    observation.notes
FROM pde.product_lifecycle_observations AS observation
ORDER BY observation.product_id, observation.source_provider_key,
         observation.observed_at DESC, observation.id DESC;
