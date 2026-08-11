// R-07 — DISTINCT that provably removes nothing.
//
// The whole key of ZORDERITEM (ORDERID, ITEMNO) is in the projection, so no two
// rows of the table can produce identical output rows. DISTINCT can only drop
// rows identical in every column including the key — rows CDC could never have
// told apart. The shape SAP ships: I_CostAnalysisResource does exactly this
// over CSKR with no joins at all.
//
// Banning the keyword outright flagged four of the six DISTINCT views S/4HANA
// ships with CDC delta declared, three of them C1-released.
//
// (Classic view because DISTINCT is not valid syntax in a view entity.)
@AbapCatalog.sqlViewName: 'ZVDISTSAFE'
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view ZI_DISTINCT_SAFE
  as select distinct from zorderitem
{
  key orderid  as OrderId,
  key itemno   as ItemNo,
      material as Material
}
