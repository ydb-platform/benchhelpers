ALTER TABLE customer
    ADD CONSTRAINT fk_customer_district
        FOREIGN KEY (c_w_id, c_d_id) REFERENCES district (d_w_id, d_id) ON DELETE CASCADE;
