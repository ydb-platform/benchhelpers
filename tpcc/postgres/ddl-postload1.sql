ALTER TABLE stock
    ADD CONSTRAINT fk_stock_warehouse
        FOREIGN KEY (s_w_id) REFERENCES warehouse (w_id) ON DELETE CASCADE,
    ADD CONSTRAINT fk_stock_item
        FOREIGN KEY (s_i_id) REFERENCES item (i_id) ON DELETE CASCADE;
