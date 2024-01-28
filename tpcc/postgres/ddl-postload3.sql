ALTER TABLE order_line
    ADD CONSTRAINT fk_order_line_oorder
        FOREIGN KEY (ol_w_id, ol_d_id, ol_o_id) REFERENCES oorder (o_w_id, o_d_id, o_id) ON DELETE CASCADE;
