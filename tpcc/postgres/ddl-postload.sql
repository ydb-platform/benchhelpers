ALTER TABLE stock
    ADD CONSTRAINT fk_stock_warehouse
        FOREIGN KEY (s_w_id) REFERENCES warehouse (w_id) ON DELETE CASCADE NOT VALID,
    ADD CONSTRAINT fk_stock_item
        FOREIGN KEY (s_i_id) REFERENCES item (i_id) ON DELETE CASCADE NOT VALID;

ALTER TABLE customer
    ADD CONSTRAINT fk_customer_district
        FOREIGN KEY (c_w_id, c_d_id) REFERENCES district (d_w_id, d_id) ON DELETE CASCADE NOT VALID;

ALTER TABLE order_line
    ADD CONSTRAINT fk_order_line_oorder
        FOREIGN KEY (ol_w_id, ol_d_id, ol_o_id) REFERENCES oorder (o_w_id, o_d_id, o_id) ON DELETE CASCADE NOT VALID,
    ADD CONSTRAINT fk_order_line_stock
        FOREIGN KEY (ol_supply_w_id, ol_i_id) REFERENCES stock (s_w_id, s_i_id) ON DELETE CASCADE NOT VALID;

ALTER TABLE district
    ADD CONSTRAINT fk_district_warehouse
        FOREIGN KEY (d_w_id) REFERENCES warehouse (w_id) ON DELETE CASCADE NOT VALID;

ALTER TABLE new_order
    ADD CONSTRAINT fk_new_order_oorder
        FOREIGN KEY (no_w_id, no_d_id, no_o_id) REFERENCES oorder (o_w_id, o_d_id, o_id) ON DELETE CASCADE NOT VALID;

ALTER TABLE oorder
    ADD CONSTRAINT fk_oorder_customer
        FOREIGN KEY (o_w_id, o_d_id, o_c_id) REFERENCES customer (c_w_id, c_d_id, c_id) ON DELETE CASCADE NOT VALID;

ALTER TABLE history
    ADD CONSTRAINT fk_history_customer
        FOREIGN KEY (h_c_w_id, h_c_d_id, h_c_id) REFERENCES customer (c_w_id, c_d_id, c_id) ON DELETE CASCADE NOT VALID,
    ADD CONSTRAINT fk_history_district
        FOREIGN KEY (h_w_id, h_d_id) REFERENCES district (d_w_id, d_id) ON DELETE CASCADE NOT VALID;

--ALTER TABLE warehouse SET LOGGED;
--ALTER TABLE item SET LOGGED;
--ALTER TABLE stock SET LOGGED;
--ALTER TABLE district SET LOGGED;
--ALTER TABLE customer SET LOGGED;
--ALTER TABLE history SET LOGGED;
--ALTER TABLE oorder SET LOGGED;
--ALTER TABLE new_order SET LOGGED;
--ALTER TABLE order_line SET LOGGED;
