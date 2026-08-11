// R-18 — ORDER_NUMBER does not exist in ZCUSTORDER, and CUSTOMER exists but is
// not a key field. Both fail at activation with exception 151054,
// "Error reading CDC annotations: mapping field does not exist".
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture: {
      mapping: [
        {table: 'ZCUSTORDER', role: #MAIN, viewElement: ['OrderId', 'Customer'], tableElement: ['ORDER_NUMBER', 'CUSTOMER']}
      ] } } }
define view entity ZI_BAD_TABLEELEMENT
  as select from zcustorder
{
  key orderid  as OrderId,
      customer as Customer,
      amount   as Amount
}
