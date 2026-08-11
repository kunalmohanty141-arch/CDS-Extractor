// R-11 — an inner join whose joined table IS in the CDC mapping.
//
// This is the shape SAP ships: of the 887 views S/4HANA declares CDC delta on,
// 38 use inner joins and 50 of those 57 joins are mapping-covered. A change in
// VBAK raises its own delta because VBAK carries its own trigger, so the join
// keyword is not what decides delta correctness — mapping coverage is.
//
// Still FAIL_FIXABLE, on R-15: the ON-condition field vbak.vbeln is not
// exposed. A separate, real defect.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture: {
      mapping: [
        {table: 'VBAP', role: #MAIN,                   viewElement: ['SalesOrder', 'Item'], tableElement: ['VBELN', 'POSNR']},
        {table: 'VBAK', role: #LEFT_OUTER_TO_ONE_JOIN, viewElement: ['SalesOrder'],         tableElement: ['VBELN']}
      ] } } }
define view entity ZI_INNER_JOIN
  as select from vbap as Item
    inner join vbak as Header on Header.vbeln = Item.vbeln
{
  key Item.vbeln   as SalesOrder,
  key Item.posnr   as Item,
      Item.matnr   as Material,
      Item.netwr   as NetValue,
      Header.erdat as CreatedOn
}
