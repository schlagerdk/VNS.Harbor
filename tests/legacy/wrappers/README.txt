The site script templates as VNS 1.3.0 left them: each calls harbor. Harbor 1.0 has no scripts in a site any more;
these are kept so the tests can still run harbor the way those scripts did (with an environment's settings in its
publish_prod.sh, as a site not migrated yet has them).
