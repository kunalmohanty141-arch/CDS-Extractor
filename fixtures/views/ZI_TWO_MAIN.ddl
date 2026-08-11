// R-16 — two #MAIN entries. Exactly one table defines row identity.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture: {
      mapping: [
        {table: 'ZCUSTORDER', role: #MAIN, viewElement: ['OrderId'], tableElement: ['ORDERID']},
        {table: 'ZORDERITEM', role: #MAIN, viewElement: ['ItemOrderId', 'ItemNo'], tableElement: ['ORDERID', 'ITEMNO']}
      ] } } }
define view entity ZI_TWO_MAIN
  as select from zcustorder as Header
    left outer to one join zorderitem as Item on Item.orderid = Header.orderid
{
  key Header.orderid  as OrderId,
      Header.customer as Customer,
      Item.orderid    as ItemOrderId,
      Item.itemno     as ItemNo
}
