-- PostgreSQL database dump
--
-- Dumped from database version 16.3
-- Dumped by pg_dump version 16.3

SET statement_timeout = 0;
SET lock_timeout = 0;

CREATE TABLE inventory (
    sku text NOT NULL,
    qty integer,
    location text
);
