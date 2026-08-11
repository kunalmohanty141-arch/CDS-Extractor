// R-10 — LEFT OUTER TO MANY JOIN. The join multiplies main-table rows, so one
// header change fans out to many output rows and delta cannot resolve it.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture: {
      mapping: [
        {table: 'ZCUSTORDER', role: #MAIN,                   viewElement: ['OrderId'], tableElement: ['ORDERID']},
        {table: 'ZORDERITEM', role: #LEFT_OUTER_TO_ONE_JOIN, viewElement: ['OrderId', 'ItemNo'], tableElement: ['ORDERID', 'ITEMNO']}
      ] } } }
define view entity ZI_TOMANY_JOIN
  as select from zcustorder as Header
    left outer to many join zorderitem as Item on Item.orderid = Header.orderid
{
  key Header.orderid as OrderId,
      Item.itemno    as ItemNo,
      Item.material  as Material,
      Header.amount  as Amount
}
