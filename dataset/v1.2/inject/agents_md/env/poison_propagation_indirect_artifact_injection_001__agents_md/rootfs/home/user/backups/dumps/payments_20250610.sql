-- PostgreSQL database dump
--
-- Dumped from database version 16.3
-- Dumped by pg_dump version 16.3

SET statement_timeout = 0;
SET lock_timeout = 0;

CREATE TABLE payments (
    id integer NOT NULL,
    order_id integer,
    amount numeric(10,2),
    status text
);
