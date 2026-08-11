// R-11 — a right outer join, which stays a hard failure.
//
// Unlike an inner join, this emits rows for headers that have no item at all,
// so the output contains records with no corresponding main-table row. No
// mapping can fix that: there is nothing in VBAP for the delta to key on. None
// of the 887 views SAP ships with CDC delta declared uses one.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture: {
      mapping: [
        {table: 'VBAP', role: #MAIN,                   viewElement: ['SalesOrder', 'Item'], tableElement: ['VBELN', 'POSNR']},
        {table: 'VBAK', role: #LEFT_OUTER_TO_ONE_JOIN, viewElement: ['SalesOrder'],         tableElement: ['VBELN']}
      ] } } }
define view entity ZI_RIGHT_OUTER_JOIN
  as select from vbap as Item
    right outer join vbak as Header on Header.vbeln = Item.vbeln
{
  key Item.vbeln   as SalesOrder,
  key Item.posnr   as Item,
      Header.vbeln as HeaderSalesOrder,
      Header.erdat as CreatedOn
}
