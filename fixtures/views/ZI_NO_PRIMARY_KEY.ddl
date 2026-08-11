// R-22 — ZNOKEY has no key beyond the client, so replication has no stable row
// identity to work with. A Datasphere Replication Flow requires a primary key.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_NO_PRIMARY_KEY
  as select from znokey
{
  payload    as Payload,
  created_at as CreatedAt
}
