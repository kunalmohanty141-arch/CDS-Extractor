// R-13 — ZORDERITEM's key is ORDERID + ITEMNO, but only ORDERID is mapped.
// This is the failure the validator exists to catch: the view activates
// cleanly and then fails at delta time, because the logging table's key is
// built from the main table's key fields.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture: {
      mapping: [
        {table: 'ZORDERITEM', role: #MAIN, viewElement: ['OrderId'], tableElement: ['ORDERID']}
      ] } } }
define view entity ZI_MISSING_MAIN_KEY
  as select from zorderitem
{
  key orderid  as OrderId,
      material as Material,
      quantity as Quantity
}
