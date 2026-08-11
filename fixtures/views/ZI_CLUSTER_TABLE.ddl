// R-21 — RFBLG is a cluster table. CDC uses database triggers, which need a
// real transparent table underneath.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_CLUSTER_TABLE
  as select from rfblg
{
  key bukrs as CompanyCode,
  key belnr as AccountingDocument,
  key gjahr as FiscalYear
}
