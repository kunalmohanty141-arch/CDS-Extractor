// R-19 — the mapping names SalesOrderId, but the view exposes OrderId.
// The classic copy-paste mapping failure.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture: {
      mapping: [
        {table: 'ZCUSTORDER', role: #MAIN, viewElement: ['SalesOrderId'], tableElement: ['ORDERID']}
      ] } } }
define view entity ZI_BAD_VIEWELEMENT
  as select from zcustorder
{
  key orderid  as OrderId,
      customer as Customer
}
