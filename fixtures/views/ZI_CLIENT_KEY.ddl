// R-23 — the client field is exposed as an ordinary key element. Note 2890171
// warns on tables with a client field as key; the framework supplies the
// client itself and a CDS view is client-dependent by default.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_CLIENT_KEY
  as select from zcustorder
{
  key mandt    as Client,
  key orderid  as OrderId,
      customer as Customer
}
