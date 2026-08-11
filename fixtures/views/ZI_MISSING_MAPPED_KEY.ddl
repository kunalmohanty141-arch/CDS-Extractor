// R-14 — the main table is mapped correctly, but the joined table's key is
// only half declared: ZORDERITEM needs ORDERID and ITEMNO.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture: {
      mapping: [
        {table: 'ZCUSTORDER', role: #MAIN,                   viewElement: ['OrderId'], tableElement: ['ORDERID']},
        {table: 'ZORDERITEM', role: #LEFT_OUTER_TO_ONE_JOIN, viewElement: ['ItemOrderId'], tableElement: ['ORDERID']}
      ] } } }
define view entity ZI_MISSING_MAPPED_KEY
  as select from zcustorder as Header
    left outer to one join zorderitem as Item on Item.orderid = Header.orderid
{
  key Header.orderid  as OrderId,
      Header.customer as Customer,
      Item.orderid    as ItemOrderId,
      Item.itemno     as ItemNo,
      Item.material   as Material
}
