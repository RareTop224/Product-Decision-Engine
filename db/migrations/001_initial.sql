CREATE SCHEMA IF NOT EXISTS pde;

CREATE TABLE IF NOT EXISTS pde.schema_migrations (
    version TEXT PRIMARY KEY,
    checksum_sha256 TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pde.data_providers (
    provider_key TEXT PRIMARY KEY,
    provider_type TEXT NOT NULL CHECK (
        provider_type IN ('golden_dataset', 'manufacturer', 'catalog', 'retailer', 'manual')
    ),
    display_name TEXT NOT NULL,
    base_url TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    terms_review_status TEXT NOT NULL DEFAULT 'not_reviewed' CHECK (
        terms_review_status IN ('not_reviewed', 'allowed_for_pilot', 'approved', 'restricted', 'rejected')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pde.import_runs (
    id UUID PRIMARY KEY,
    provider_key TEXT NOT NULL REFERENCES pde.data_providers(provider_key),
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    source_revision TEXT,
    counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS pde.products (
    id TEXT PRIMARY KEY,
    manufacturer TEXT NOT NULL,
    model TEXT NOT NULL,
    mpn TEXT,
    product_type TEXT NOT NULL CHECK (product_type IN ('printer', 'mfp')),
    print_technology TEXT NOT NULL,
    color_mode TEXT NOT NULL CHECK (color_mode IN ('mono', 'color')),
    wifi BOOLEAN NOT NULL,
    auto_duplex BOOLEAN NOT NULL,
    recommended_monthly_volume INTEGER CHECK (recommended_monthly_volume > 0),
    expected_consumable_channels TEXT[] NOT NULL CHECK (
        cardinality(expected_consumable_channels) > 0
    ),
    maintenance_data_status TEXT NOT NULL CHECK (
        maintenance_data_status IN ('complete', 'not_published', 'incomplete')
    ),
    status TEXT NOT NULL,
    dataset_position INTEGER NOT NULL CHECK (dataset_position >= 0),
    last_import_run_id UUID REFERENCES pde.import_runs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (manufacturer, model)
);

CREATE TABLE IF NOT EXISTS pde.consumables (
    id TEXT PRIMARY KEY,
    manufacturer TEXT NOT NULL,
    part_number TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('toner', 'cartridge', 'ink_bottle', 'drum', 'maintenance_box', 'other')
    ),
    color TEXT NOT NULL,
    yield_value INTEGER NOT NULL CHECK (yield_value > 0),
    yield_unit TEXT NOT NULL,
    yield_standard TEXT,
    is_oem BOOLEAN NOT NULL,
    dataset_position INTEGER NOT NULL CHECK (dataset_position >= 0),
    last_import_run_id UUID REFERENCES pde.import_runs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pde.product_consumables (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES pde.products(id),
    consumable_id TEXT NOT NULL REFERENCES pde.consumables(id),
    role TEXT NOT NULL CHECK (role IN ('starter', 'replacement', 'maintenance')),
    channel TEXT NOT NULL,
    page_scope TEXT NOT NULL CHECK (page_scope IN ('mono_pages', 'color_pages', 'all_pages')),
    quantity_in_box INTEGER NOT NULL DEFAULT 1 CHECK (quantity_in_box > 0),
    mono_page_weight INTEGER NOT NULL DEFAULT 1 CHECK (mono_page_weight >= 0),
    color_page_weight INTEGER NOT NULL DEFAULT 1 CHECK (color_page_weight >= 0),
    installed_yield_value INTEGER CHECK (installed_yield_value > 0),
    notes TEXT,
    dataset_position INTEGER NOT NULL CHECK (dataset_position >= 0),
    last_import_run_id UUID REFERENCES pde.import_runs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (mono_page_weight > 0 OR color_page_weight > 0),
    UNIQUE (product_id, role, channel, consumable_id)
);

CREATE TABLE IF NOT EXISTS pde.evidence (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (
        entity_type IN ('product', 'consumable', 'product_consumable', 'price_observation')
    ),
    entity_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    observed_at DATE NOT NULL,
    verification_status TEXT NOT NULL CHECK (
        verification_status IN ('candidate', 'verified', 'conflict', 'rejected')
    ),
    notes TEXT,
    dataset_position INTEGER NOT NULL CHECK (dataset_position >= 0),
    last_import_run_id UUID REFERENCES pde.import_runs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS evidence_entity_field_idx
    ON pde.evidence(entity_type, entity_id, field_name);

CREATE TABLE IF NOT EXISTS pde.price_observations (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('product', 'consumable')),
    entity_id TEXT NOT NULL,
    price_rub INTEGER NOT NULL CHECK (price_rub >= 0),
    source_evidence_id TEXT NOT NULL REFERENCES pde.evidence(id),
    observed_at DATE NOT NULL,
    is_primary BOOLEAN NOT NULL DEFAULT TRUE,
    dataset_position INTEGER NOT NULL CHECK (dataset_position >= 0),
    last_import_run_id UUID REFERENCES pde.import_runs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS one_primary_price_per_entity_idx
    ON pde.price_observations(entity_type, entity_id)
    WHERE is_primary;

CREATE INDEX IF NOT EXISTS price_observation_lookup_idx
    ON pde.price_observations(entity_type, entity_id, observed_at DESC);

CREATE OR REPLACE FUNCTION pde.validate_price_observation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.entity_type = 'product' AND NOT EXISTS (
        SELECT 1 FROM pde.products WHERE id = NEW.entity_id
    ) THEN
        RAISE EXCEPTION 'Unknown product for price observation %: %', NEW.id, NEW.entity_id;
    END IF;
    IF NEW.entity_type = 'consumable' AND NOT EXISTS (
        SELECT 1 FROM pde.consumables WHERE id = NEW.entity_id
    ) THEN
        RAISE EXCEPTION 'Unknown consumable for price observation %: %', NEW.id, NEW.entity_id;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pde.evidence
        WHERE id = NEW.source_evidence_id
          AND entity_type = 'price_observation'
          AND entity_id = NEW.id
          AND field_name = 'price_rub'
    ) THEN
        RAISE EXCEPTION 'Invalid source evidence for price observation %: %',
            NEW.id, NEW.source_evidence_id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS validate_price_observation_trigger
    ON pde.price_observations;
CREATE TRIGGER validate_price_observation_trigger
BEFORE INSERT OR UPDATE ON pde.price_observations
FOR EACH ROW EXECUTE FUNCTION pde.validate_price_observation();

CREATE TABLE IF NOT EXISTS pde.usage_scenarios (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mono_pages_per_month INTEGER NOT NULL CHECK (mono_pages_per_month >= 0),
    color_pages_per_month INTEGER NOT NULL CHECK (color_pages_per_month >= 0),
    ownership_months INTEGER NOT NULL CHECK (ownership_months > 0),
    max_purchase_price_rub INTEGER CHECK (max_purchase_price_rub >= 0),
    require_mfp BOOLEAN NOT NULL DEFAULT FALSE,
    require_wifi BOOLEAN NOT NULL DEFAULT FALSE,
    require_auto_duplex BOOLEAN NOT NULL DEFAULT FALSE,
    dataset_position INTEGER NOT NULL CHECK (dataset_position >= 0),
    last_import_run_id UUID REFERENCES pde.import_runs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pde.retailer_basket_audits (
    id TEXT PRIMARY KEY,
    retailer TEXT NOT NULL,
    observed_at DATE NOT NULL,
    source_type TEXT NOT NULL,
    verification_status TEXT NOT NULL CHECK (
        verification_status IN ('candidate', 'verified', 'conflict', 'rejected')
    ),
    scenario_id TEXT NOT NULL REFERENCES pde.usage_scenarios(id),
    dataset_position INTEGER NOT NULL CHECK (dataset_position >= 0),
    last_import_run_id UUID REFERENCES pde.import_runs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pde.retailer_product_offers (
    audit_id TEXT NOT NULL REFERENCES pde.retailer_basket_audits(id) ON DELETE CASCADE,
    product_id TEXT NOT NULL REFERENCES pde.products(id),
    device_availability TEXT NOT NULL CHECK (
        device_availability IN (
            'in_stock', 'orderable_unconfirmed', 'expected', 'transit',
            'unavailable', 'not_listed', 'unverified'
        )
    ),
    device_price_rub INTEGER CHECK (device_price_rub >= 0),
    device_source_url TEXT,
    notes TEXT,
    dataset_position INTEGER NOT NULL CHECK (dataset_position >= 0),
    PRIMARY KEY (audit_id, product_id)
);

CREATE TABLE IF NOT EXISTS pde.retailer_offer_consumables (
    audit_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    consumable_id TEXT NOT NULL REFERENCES pde.consumables(id),
    is_required BOOLEAN NOT NULL,
    is_covered BOOLEAN NOT NULL,
    source_url TEXT,
    price_rub INTEGER CHECK (price_rub >= 0),
    dataset_position INTEGER NOT NULL CHECK (dataset_position >= 0),
    PRIMARY KEY (audit_id, product_id, consumable_id),
    FOREIGN KEY (audit_id, product_id)
        REFERENCES pde.retailer_product_offers(audit_id, product_id)
        ON DELETE CASCADE,
    CHECK (NOT is_covered OR is_required),
    CHECK (NOT is_covered OR source_url IS NOT NULL),
    CHECK (price_rub IS NULL OR is_covered)
);

CREATE TABLE IF NOT EXISTS pde.availability_observations (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES pde.products(id),
    source_provider_key TEXT NOT NULL REFERENCES pde.data_providers(provider_key),
    availability TEXT NOT NULL CHECK (
        availability IN (
            'in_stock', 'orderable_unconfirmed', 'expected', 'transit',
            'unavailable', 'not_listed', 'unverified'
        )
    ),
    observed_at DATE NOT NULL,
    source_url TEXT,
    verification_status TEXT NOT NULL CHECK (
        verification_status IN ('candidate', 'verified', 'conflict', 'rejected')
    ),
    retailer_basket_audit_id TEXT REFERENCES pde.retailer_basket_audits(id),
    dataset_position INTEGER NOT NULL CHECK (dataset_position >= 0),
    last_import_run_id UUID REFERENCES pde.import_runs(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS availability_observation_lookup_idx
    ON pde.availability_observations(
        product_id, source_provider_key, observed_at DESC
    );

CREATE OR REPLACE VIEW pde.latest_product_availability AS
SELECT DISTINCT ON (observation.product_id, observation.source_provider_key)
    observation.product_id,
    observation.source_provider_key,
    observation.observed_at,
    observation.availability,
    observation.source_url,
    observation.verification_status
FROM pde.availability_observations AS observation
ORDER BY observation.product_id, observation.source_provider_key,
         observation.observed_at DESC, observation.id DESC;
