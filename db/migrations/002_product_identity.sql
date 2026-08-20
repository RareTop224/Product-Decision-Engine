ALTER TABLE pde.products
    DROP CONSTRAINT IF EXISTS products_manufacturer_model_key;

CREATE UNIQUE INDEX IF NOT EXISTS unique_product_mpn_idx
    ON pde.products(manufacturer, mpn)
    WHERE mpn IS NOT NULL;
