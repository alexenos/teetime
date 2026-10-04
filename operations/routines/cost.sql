-- GCP spend for one invoice month, by service. Read by the cost Routine
-- (operations/routines/cost.md); dataset created in terraform/cost.tf.
--
--   bq query --use_legacy_sql=false --format=json \
--     --parameter=invoice_month:STRING:202610 < operations/routines/cost.sql
--
-- Not yet run against real rows: written against Google's documented detailed
-- export schema before the export held any data.
--
-- invoice.month, not usage dates: it is the month a charge is billed in, which is
-- what the Console's monthly figure reports, and it absorbs late adjustments.
--
-- The table name carries the billing account id. The wildcard avoids writing that
-- id into a public repository; the detailed export creates exactly one table.
--
-- The billing account may pay for other projects, so the filter on project.id is
-- what makes this TeeTime's spend rather than the account's.
--
-- net is what is owed: cost plus credits, and credits are negative. currency
-- is returned rather than assumed; a row whose currency is not USD is not a USD
-- figure.
SELECT
  service.description AS service,
  currency,
  SUM(cost) AS gross,
  SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0)) AS net
FROM `gen-lang-client-0822973627.billing_export.gcp_billing_export_resource_v1_*`
WHERE invoice.month = @invoice_month
  AND project.id = 'gen-lang-client-0822973627'
GROUP BY service, currency
ORDER BY net DESC
