// R-20 — the mapping addresses ZI_CUSTORDER, which is a CDS view, not a table.
// For stacked views the mapping must name the underlying database tables
// (Appendix A.5). Silent and common.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture: {
      mapping: [
        {table: 'ZI_CUSTORDER', role: #MAIN, viewElement: ['OrderId'], tableElement: ['ORDERID']}
      ] } } }
define view entity ZI_MAPPED_VIEW
  as select from ZI_CUSTORDER as Orders
{
  key Orders.OrderId  as OrderId,
      Orders.Customer as Customer,
      Orders.Amount   as Amount
}
