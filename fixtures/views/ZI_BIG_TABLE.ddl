// R-30 — above the documented ~2bn row ceiling. A warning, not a failure:
// the view is valid, but the extraction will hit an internal HANA limit.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_BIG_TABLE
  as select from zbigtable
{
  key record_id as RecordId,
      payload   as Payload
}
