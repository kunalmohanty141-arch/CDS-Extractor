// R-07 — DISTINCT that can genuinely merge two different base records.
//
// ZORDERITEM's key is ORDERID + ITEMNO, and only ORDERID is exposed. Two items
// of the same order with the same material collapse into one row, so a change
// to either becomes indistinguishable. That is the case the rule exists for.
//
// Compare ZI_DISTINCT_SAFE, where the whole key is projected and DISTINCT can
// remove nothing.
//
// (Also syntactically invalid in a view entity; kept as a classic view so the
//  fixture exercises R-07 rather than a parser error.)
@AbapCatalog.sqlViewName: 'ZVDISTINCT'
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view ZI_DISTINCT
  as select distinct from zorderitem
{
  key orderid  as OrderId,
      material as Material
}
