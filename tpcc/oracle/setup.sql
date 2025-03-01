-- implies that tpcc container database has been created
alter session set container=tpcc;
-- separate tablespace for tpcc data
create bigfile tablespace tpcc1;
-- separate user account for tpcc test
create user tpcc identified by "P@$$w0rd+" default tablespace tpcc1 temporary tablespace temp;
alter user tpcc quota unlimited on tpcc1;
grant connect, create session, create table to tpcc;
-- optional, not needed for tpcc test
grant select_catalog_role to tpcc;

-- command examples:
-- 1. just create the TPCC tables:
--    ./run_oracle.sh --warehouses 100 --config sample.xml --hosts sample-hosts.txt --no-load --no-run
